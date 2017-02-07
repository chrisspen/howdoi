#!/usr/bin/env python
from __future__ import print_function

######################################################
#
# howdou - instant coding answers via the command line
# written by Benjamin Gleitzman (gleitz@mit.edu)
# inspired by Rich Jones (rich@anomos.info)
#
######################################################

import argparse
import datetime
import glob
import os
import random
import re
import sys
import hashlib

import requests
from requests.exceptions import ConnectionError # pylint: disable=redefined-builtin
from requests.exceptions import SSLError

import requests_cache

from six import text_type, string_types

try:
    from urllib.parse import quote as url_quote
except ImportError:
    from urllib import quote as url_quote

try:
    from urllib import getproxies
except ImportError:
    from urllib.request import getproxies

from pygments import highlight
from pygments.lexers import guess_lexer, get_lexer_by_name
from pygments.formatters import TerminalFormatter # pylint: disable=no-name-in-module
from pygments.util import ClassNotFound

from pyquery import PyQuery as pq

import yaml
from elasticsearch import Elasticsearch
from elasticsearch.exceptions import NotFoundError#, TransportError
import dateutil.parser

from lockfile import LockFile, LockTimeout

# Handle unicode between Python 2 and 3
# http://stackoverflow.com/a/6633040/305414
if sys.version < '3':
    import codecs
    def u(x):
        return codecs.unicode_escape_decode(x)[0]
else:
    def u(x):
        return x

KNOWLEDGEBASE_FN = os.path.expanduser(os.getenv('HOWDOU_KB', '~/.howdou.yml'))
KNOWLEDGEBASE_INDEX = os.getenv('HOWDOU_INDEX', 'howdou')
KNOWLEDGEBASE_TIMESTAMP_FN = os.path.expanduser(os.getenv('HOWDOU_TIMESTAMP', '~/.howdou_last'))

APP_DATA_DIR = os.path.expanduser(os.getenv('HOWDOU_DIR', '~/.howdou'))

LOCKFILE_PATH = os.path.expanduser(os.getenv('HOWDOU_LOCKFILE', '~/.howdou_lock'))

if os.getenv('HOWDOU_DISABLE_SSL'):  # Set http instead of https
    SEARCH_URL = 'http://www.google.com/search?q=site:{0}%20{1}'
else:
    SEARCH_URL = 'https://www.google.com/search?q=site:{0}%20{1}'

LOCALIZATION = os.getenv('HOWDOU_LOCALIZATION') or 'en'

LOCALIZATON_URLS = {
    'en': 'stackoverflow.com',
    'pt-br': 'pt.stackoverflow.com',
}

USER_AGENTS = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10.7; rv:11.0) Gecko/20100101 Firefox/11.0',
               'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:22.0) Gecko/20100 101 Firefox/22.0',
               'Mozilla/5.0 (Windows NT 6.1; rv:11.0) Gecko/20100101 Firefox/11.0',
               'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_7_4) AppleWebKit/536.5 (KHTML, like Gecko) Chrome/19.0.1084.46 Safari/536.5',
               'Mozilla/5.0 (Windows; Windows NT 6.1) AppleWebKit/536.5 (KHTML, like Gecko) Chrome/19.0.1084.46 Safari/536.5',)
ANSWER_HEADER = u('--- Answer {0} ---\n{1}')
NO_ANSWER_MSG = '< no answer given >'
XDG_CACHE_DIR = os.environ.get('XDG_CACHE_HOME',
                               os.path.join(os.path.expanduser('~'), '.cache'))
CACHE_DIR = os.path.join(XDG_CACHE_DIR, 'howdou')
CACHE_FILE = os.path.join(CACHE_DIR, 'cache{0}'.format(
        sys.version_info[0] if sys.version_info[0] == 3 else ''))

def touch(fname, times=None):
    with open(fname, 'a'):
        os.utime(fname, times)

def delete():
    """
    Forcibly deletes the index from the server.
    """
    print('delete.KNOWLEDGEBASE_INDEX:', KNOWLEDGEBASE_INDEX)
    es = Elasticsearch()
    es.indices.delete(index=KNOWLEDGEBASE_INDEX, ignore=[400, 404])

def is_kb_updated():
    """
    Returns true if the knowledge base file has changed since the last run.
    """
    if not os.path.isfile(KNOWLEDGEBASE_TIMESTAMP_FN):
        return True
    kb_last_modified = datetime.datetime.fromtimestamp(os.path.getmtime(KNOWLEDGEBASE_FN))
    timestamp_last_modified = datetime.datetime.fromtimestamp(os.path.getmtime(KNOWLEDGEBASE_TIMESTAMP_FN))
    modified = kb_last_modified > timestamp_last_modified
    return modified

def update_kb_timestamp():
    touch(KNOWLEDGEBASE_TIMESTAMP_FN)

def get_proxies():
    proxies = getproxies()
    filtered_proxies = {}
    for key, value in proxies.items():
        if key.startswith('http'):
            if not value.startswith('http'):
                filtered_proxies[key] = 'http://%s' % value
            else:
                filtered_proxies[key] = value
    return filtered_proxies


def get_result(url):
    try:
        return requests.get(url, headers={'User-Agent': random.choice(USER_AGENTS)}, proxies=get_proxies()).text
    except requests.exceptions.SSLError as e:
        print('[ERROR] Encountered an SSL Error. Try using HTTP instead of '
              'HTTPS by setting the environment variable "HOWDOU_DISABLE_SSL".\n')
        raise e


def is_question(link):
    return re.search(r'questions/\d+/', link)


def get_links(query):
    localization_url = LOCALIZATON_URLS[LOCALIZATION]
    result = get_result(SEARCH_URL.format(localization_url, url_quote(query)))
    html = pq(result)
    return [a.attrib['href'] for a in html('.l')] or \
        [a.attrib['href'] for a in html('.r')('a')]


def get_link_at_pos(links, position):
    links = [link for link in links if is_question(link)]
    if not links:
        return False

    if len(links) >= position:
        link = links[position-1]
    else:
        link = links[-1]
    return link


def format_output(code, args):
    if not args['color']:
        return code
    lexer = None

    # try to find a lexer using the StackOverflow tags
    # or the query arguments
    for keyword in args['query'].split() + args['tags']:
        try:
            lexer = get_lexer_by_name(keyword)
            break
        except ClassNotFound:
            pass

    # no lexer found above, use the guesser
    if not lexer:
        lexer = guess_lexer(code)

    return highlight(code,
                     lexer,
                     TerminalFormatter(bg='dark'))


def get_answer(args, links):
    """
    Given search arguments and a links of web links (usually Stackoverflow),
    find the best answer to the search question.
    """
    print('get_answer: args:', args, 'links:', links)
    link = get_link_at_pos(links, args['pos'])
    if not link:
        return False, None
        
    # Don't lookup answer text, just return link.
    if args.get('link'):
        return None, link
        
    page = get_result(link + '?answertab=votes')
    html = pq(page)

    first_answer = html('.answer').eq(0)
    instructions = first_answer.find('pre') or first_answer.find('code')
    args['tags'] = [t.text for t in html('.post-tag')]

    if not instructions and not args['all']:
        text = first_answer.find('.post-text').eq(0).text()
    elif args['all']:
        texts = []
        for html_tag in first_answer.items('.post-text > *'):
            current_text = html_tag.text()
            if current_text:
                if html_tag[0].tag in ['pre', 'code']:
                    texts.append(format_output(current_text, args))
                else:
                    texts.append(current_text)
        texts.append('\n---\nAnswer from {0}'.format(link))
        text = '\n'.join(texts)
    else:
        text = format_output(instructions.eq(0).text(), args)
    if text is None:
        text = NO_ANSWER_MSG
    text = text.strip()
    return text, link

def get_instructions(args):
    answers = []
    ignore_remote = args['ignore_remote']
    ignore_local = args['ignore_local']
    append_header = args['num_answers'] > 1 \
        or args['show_score'] or args['show_source']
    initial_position = args['pos']
    query = args['query']
    if not query:
        return ''
    
    # Check local index first.
    #http://elasticsearch.org/guide/reference/query-dsl/
    #http://www.elasticsearch.org/guide/en/elasticsearch/reference/current/query-dsl-query-string-query.html
    if not ignore_local:
        es = Elasticsearch()
        print('create.KNOWLEDGEBASE_INDEX:', KNOWLEDGEBASE_INDEX)
        es.indices.create(index=KNOWLEDGEBASE_INDEX, ignore=400)
        try:
            results = es.search(
                index=KNOWLEDGEBASE_INDEX,
                body={
                    'query':{
        ##                'query_string':{ # search all text fields
        ##                    'query':query,
        ##                },
        #                'field':{
        #                    'questions':{
        #                        'query':query,
        #                    }
        #                },
                        "function_score": {
                            "query": {  
                                "match": {
                                    "questions": query
                                }
                            },
                            "functions": [{
                                "script_score": { 
                                    "script": "doc['weight'].value"
                                }
                            }],
                            "score_mode": "multiply"
                        }
                    },
        #            'query':{
        #                'field':{
        #                    'questions':{
        #                        'query':query,
        #                    }
        #                },
        #            },
                },
            )
            
        #    pprint(results['hits']['hits'],indent=4)
            hits = results['hits']['hits'][:args['num_answers']]
            if hits:
                answer_number = -1
                for hit in hits:
                    #print('hit',hit)
                    answer_number += 1
                    current_position = answer_number + initial_position
                    answer = hit['_source']['answer'].strip()
                    #TODO:sort/boost by weight?
                    #TODO:ignore low weights?
                    score = hit['_score']
                    if score < float(args['min_score']):
                        continue
                    answer_prefixes = []
                    
                    if args['show_score']:
                        answer_prefixes.append('score: %s' % score)
                        
                    if args['show_source']:
                        source = (hit['_source'].get('source') or '').strip() or None
                        answer_prefixes.append('source: %s (local)' % source)
                        
                    if append_header:
                        if answer_prefixes:
                            answer = '\n'.join(answer_prefixes) + '\n\n' + answer
                        answer = ANSWER_HEADER.format(current_position, answer)
                    answer = answer + '\n'
                    answers.append(answer)
                    
        except NotFoundError as e:
            print('Local lookup error:', file=sys.stderr)
            print(e, file=sys.stderr)
            raise
    
    # If we found nothing satisfying locally, then search the net.
    if not answers and not ignore_remote:
        links = get_links(query)
        if not links:
            return False
        for answer_number in range(args['num_answers']):
            current_position = answer_number + initial_position
            args['pos'] = current_position
            result = get_answer(args, links)
            print('result:', result)
            answer, link = result
            if not answer and not link:
                continue
            answer_prefixes = []
            if append_header:
                answer_prefixes.append('source: %s (remote)' % link)
                if answer_prefixes:
                    answer = '\n'.join(answer_prefixes) + '\n\n' + (answer or '')
                answer = ANSWER_HEADER.format(current_position, answer)
            answer = answer + '\n'
            answers.append(answer)
            
    return '\n'.join(answers)


def enable_cache():
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)
    requests_cache.install_cache(CACHE_FILE)


def clear_cache():
    for cache in glob.glob('{0}*'.format(CACHE_FILE)):
        os.remove(cache)


def howdou(args):
    args['query'] = ' '.join(args['query']).replace('?', '')
    if not args['query']:
        return ''
    try:
        return get_instructions(args) or 'Sorry, couldn\'t find any help with that topic\n'
    except (ConnectionError, SSLError):
        return 'Failed to establish network connection\n'

def init_kb():
    kb_fn = os.path.expanduser(KNOWLEDGEBASE_FN)
    if not os.path.isfile(kb_fn):
        open(kb_fn, 'w').write('''-   questions:
    -   how do I create a new howdou knowledge base entry
    tags:
        context: howdou
    answers:
    -   weight: 1
        date: 2014-2-22
        source: 
        formatter: 
        text: |
            nano ~/.howdou.yml
            howdou --reindex
''')

def get_text_hash(text):
    """
    Returns the hash of the given text.
    """
    h = hashlib.sha512()
    if not isinstance(text, text_type):
        text = text_type(text, encoding='utf-8', errors='replace')
    h.update(text.encode('utf-8', 'replace'))
    return h.hexdigest()

def mark_indexed(question_str, answer_str):
    hash_fn = os.path.join(APP_DATA_DIR, get_text_hash(question_str))
    hash_contents = get_text_hash(answer_str)
    open(hash_fn, 'w').write(hash_contents)

def is_indexed(question_str, answer_str):
    """
    Returns true if this exact combination has been previously indexed.
    Returns false otherwise.
    """
    hash_fn = os.path.join(APP_DATA_DIR, get_text_hash(question_str))
    if not os.path.isfile(hash_fn):
        return False
    hash_contents = get_text_hash(answer_str)
    if open(hash_fn).read() != hash_contents:
        return False
    return True

def index_kb(force=False):
    #print('')
    es = Elasticsearch()
    count = 0
    
    if not os.path.isdir(APP_DATA_DIR):
        os.mkdir(APP_DATA_DIR)
    
    # Count total combinations so we can accurately measure progress.
    total = 0
    for item in yaml.load(open(os.path.expanduser(KNOWLEDGEBASE_FN))):
        for answer in item['answers']:
            total += 1
    
    if force:
        delete()
    
    for item in yaml.load(open(os.path.expanduser(KNOWLEDGEBASE_FN))):
        #print('questions:', item['questions'])
        questions = u'\n'.join(map(text_type, item['questions']))
        for answer in item['answers']:
            count += 1
            sys.stdout.write('\rRe-indexing %i of %i...' % (count, total))
            sys.stdout.flush()
            
            if not force and is_indexed(questions, answer['text']):
                continue
            
            weight = float(answer.get('weight', 1))
            dt = answer['date']
            if isinstance(dt, string_types):
                try:
                    dt = dateutil.parser.parse(dt)
                except ValueError as e:
                    raise Exception('Invalid date: %s' % dt)
                
            # Register this combination in the database.
            es.index(
                index=KNOWLEDGEBASE_INDEX,
                doc_type='text',
                id=count,
#                properties=dict(
#                    text=dict(type='string', boost=weight)
#                ),
                body=dict(
                    questions=questions,
                    answer=answer['text'],
                    source=answer.get('source', ''),
                    text=questions + ' ' + answer['text'],
                    action_subject=answer.get('action_subject'),
                    timestamp=dt,
                    weight=weight,
                ),
            )
            
            # Record a hash of this combination so we can skip it next time.
            mark_indexed(questions, answer['text'])
            
    es.indices.refresh(index=KNOWLEDGEBASE_INDEX)
    update_kb_timestamp()
    print('\nRe-indexed %i items.' % (count,))

def get_parser():
    parser = argparse.ArgumentParser(description='instant coding answers via the command line')
    parser.add_argument(
        'query', metavar='QUERY', type=str, nargs='*',
        help='the question to answer')
    parser.add_argument(
        '-p', '--pos',
        help='select answer in specified position (default: 1)',
        default=1, type=int)
    parser.add_argument(
        '-a', '--all', help='display the full text of the answer',
        action='store_true')
    parser.add_argument(
        '-l', '--link', help='display only the answer link',
        action='store_true')
    parser.add_argument(
        '-c', '--color', help='enable colorized output',
        action='store_true')
    parser.add_argument(
        '-n', '--num-answers',
        help='number of answers to return',
        default=1, type=int)
    parser.add_argument(
        '--min-score',
        help='the minimum score accepted on local answers',
        default=1, type=float)
    parser.add_argument(
        '-C', '--clear-cache', help='clear the cache',
        action='store_true')
    parser.add_argument(
        '--reindex', help='refresh the elasticsearch index',
        default=False,
        action='store_true')
    parser.add_argument(
        '--force',
        help='Used with --reindex, forces reindexing of all items even if no change was made',
        default=False,
        action='store_true')
    parser.add_argument(
        '--just-check', help='re-indexes if neessary',
        default=False,
        action='store_true')
    parser.add_argument(
        '--ignore-local',
        help='ignore local cache',
        default=False,
        action='store_true')
    parser.add_argument(
        '--ignore-remote',
        help='ignore remote',
        default=False,
        action='store_true')
    parser.add_argument(
        '--show-score',
        help='display score of all results',
        default=False,
        action='store_true')
    parser.add_argument(
        '--hide-source',
        help='displays any source linked to the answer',
        dest='show_source',
        default=True,
        action='store_false')
    return parser

def command_line_runner():
    parser = get_parser()
    args = vars(parser.parse_args())

    if args['clear_cache']:
        clear_cache()
        print('Cache cleared successfully')
        return

    init_kb()
    if args['reindex'] or is_kb_updated():
        index_kb(force=args['force'])

    if not args['query'] and not args['reindex'] and not args['just_check']:
        parser.print_help()
        return

    # enable the cache if user doesn't want it to be disabled
    if not os.getenv('HOWDOU_DISABLE_CACHE'):
        enable_cache()

    if not args['just_check']:
        print
        if sys.version < '3':
            print(howdou(args).encode('utf-8', 'ignore'))
        else:
            print(howdou(args))

if __name__ == '__main__':
    lock = LockFile(LOCKFILE_PATH)
    try:
        lock.acquire(timeout=60) # wait in seconds
        command_line_runner()
    except LockTimeout:
        #lock.break_lock()
        #lock.acquire()
        print('Lock timeout.', file=sys.stderr)
        sys.exit(1)
    finally:
        lock.release()
        