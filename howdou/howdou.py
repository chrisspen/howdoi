#!/usr/bin/env python
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import argparse
import datetime
import glob
import os
import re
import sys
import hashlib
from pprint import pprint

import requests
#from requests.exceptions import ConnectionError # pylint: disable=redefined-builtin
from requests.exceptions import SSLError

import requests_cache

#https://pythonhosted.org/six/
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
#from elasticsearch.exceptions import NotFoundError
import dateutil.parser

from fake_useragent import UserAgent

import fasteners

#from howdou import __version__
from .__init__ import __version__

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
CACHE_DIR = os.path.join(os.path.join(os.path.expanduser('~'), '.cache'), 'howdou')

KNOWLEDGEBASE_STUB = '''-   questions:
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
'''

if os.getenv('HOWDOU_DISABLE_SSL'): # Set http instead of https
    SEARCH_URL = 'http://www.google.com/search?q=site:{0}%20{1}'
else:
    SEARCH_URL = 'https://www.google.com/search?q=site:{0}%20{1}'

LOCALIZATION = os.getenv('HOWDOU_LOCALIZATION') or 'en'

LOCALIZATON_URLS = {
    'en': 'stackoverflow.com',
    'pt-br': 'pt.stackoverflow.com',
}

ANSWER_HEADER = u('--- Answer {i} --- score: {score} --- weight: {weight} --- {source} ---\n\n{answer}')

NO_ANSWER_MSG = '< no answer given >'

QUERY = 'query'
REINDEX = 'reindex'
CLEAR_CACHE = 'clear-cache'
ACTIONS = (QUERY, REINDEX, CLEAR_CACHE)

DEFAULT_USERAGENT = 'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:51.0) Gecko/20100101 Firefox/51.0'

ua = UserAgent(fallback=DEFAULT_USERAGENT)

def get_link_at_pos(links, position):
        
    def is_question(link):
        return re.search(r'questions/\d+/', link)
        
    links = [link for link in links if is_question(link)]
    if not links:
        return False

    if len(links) >= position:
        link = links[position-1]
    else:
        link = links[-1]
    return link

def touch(fname, times=None):
    with open(fname, 'a'):
        os.utime(fname, times)

def get_text_hash(text):
    """
    Returns the hash of the given text.
    """
    h = hashlib.sha512()
    if not isinstance(text, text_type):
        text = text_type(text, encoding='utf-8', errors='replace')
    h.update(text.encode('utf-8', 'replace'))
    return h.hexdigest()

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

def find_true_link(s):
    """
    Sometimes Google wraps our links inside sneaky tracking links, which often fail and slow us down
    so remove them. 
    """
    # Convert "/url?q=<real_url>" to "<real_url>".
    if s and s.startswith('/') and 'http' in s:
        s = s[s.find('http'):]
    return s

class HowDoU(object):
    
    def __init__(self, **kwargs):
        kwargs.setdefault('verbose', False)
        self.__dict__.update(kwargs)
        
        if self.verbose:
            print('kwargs:')
            pprint(kwargs, indent=4)
        assert self.action in ACTIONS, 'Invalid action "%s". Must be one of %s' % (self.action, ', '.join(ACTIONS))
        
        self.cache_file = os.path.join(self.cache_dir, 'cache')

        self.query = (' '.join(self.query).replace('?', '')).strip()
        
        self.kb_filename = os.path.expanduser(self.kb_filename)
        self.kb_timestamp = os.path.expanduser(self.kb_timestamp)
        self.kb_app_dir = os.path.expanduser(self.kb_app_dir)
        
        self.append_header = False
        
        self.last_reindex_count = 0

    def delete_index(self):
        """
        Forcibly deletes the index from the server.
        """
        print('Deleting index %s...' % self.kb_index_name)
        es = Elasticsearch()
        es.indices.delete(index=self.kb_index_name, ignore=[400, 404])
        print('Deleting index cache at %s...' % self.kb_app_dir)
        os.system('rm -Rf %s/*' % self.kb_app_dir)
    
    def is_kb_updated(self):
        """
        Returns true if the knowledge base file has changed since the last run.
        """
        if not os.path.isfile(self.kb_timestamp):
            print('First-time indexing required.')
            return True
        kb_last_modified = datetime.datetime.fromtimestamp(os.path.getmtime(self.kb_filename))
        timestamp_last_modified = datetime.datetime.fromtimestamp(os.path.getmtime(self.kb_timestamp))
        modified = kb_last_modified > timestamp_last_modified
        if modified:
            print('Changes found.')
        return modified
    
    def update_kb_timestamp(self):
        touch(self.kb_timestamp)

    def get_result(self, url):
        try:
            return requests.get(url, headers={'User-Agent': ua.random}, proxies=get_proxies()).text
        except SSLError as e:
            print('[ERROR] Encountered an SSL Error. Try using HTTP instead of '
                  'HTTPS by setting the environment variable "HOWDOU_DISABLE_SSL".\n')
            raise e
    
    def get_links(self, query):
        localization_url = LOCALIZATON_URLS[self.lang]
        result = self.get_result(SEARCH_URL.format(localization_url, url_quote(query)))
        html = pq(result)
        return [a.attrib['href'] for a in html('.l')] or [a.attrib['href'] for a in html('.r')('a')]

    def format_output(self, code):
        if not self.color:
            return code
        lexer = None
    
        # try to find a lexer using the StackOverflow tags
        # or the query arguments
        for keyword in self.query.split() + self.tags:
            try:
                lexer = get_lexer_by_name(keyword)
                break
            except ClassNotFound:
                pass
    
        # no lexer found above, use the guesser
        if not lexer:
            lexer = guess_lexer(code)
    
        return highlight(code, lexer, TerminalFormatter(bg='dark'))

    def get_answer(self, links):
        """
        Given search arguments and a links of web links (usually Stackoverflow),
        find the best answer to the search question.
        """
        #print('get_answer: args:', args, 'links:', links)
        link = get_link_at_pos(links, self.pos)
        if not link:
            return False, None
            
        # Don't lookup answer text, just return link.
        if self.link:
            return None, link

        page = self.get_result(find_true_link(link) + '?answertab=votes')
        html = pq(page)
    
        first_answer = html('.answer').eq(0)
        instructions = first_answer.find('pre') or first_answer.find('code')
        self.tags = [t.text for t in html('.post-tag')]
    
        if not instructions and not self.all:
            text = first_answer.find('.post-text').eq(0).text()
        elif self.all:
            texts = []
            for html_tag in first_answer.items('.post-text > *'):
                current_text = html_tag.text()
                if current_text:
                    if html_tag[0].tag in ['pre', 'code']:
                        texts.append(self.format_output(current_text))
                    else:
                        texts.append(current_text)
            texts.append('\n---\nAnswer from {0}'.format(link))
            text = '\n'.join(texts)
        else:
            text = self.format_output(instructions.eq(0).text())
        if text is None:
            text = NO_ANSWER_MSG
        text = text.strip()
        return text, link

    def enable_cache(self):
        if not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir)
        requests_cache.install_cache(self.cache_file)

    def clear_cache(self):
        for cache in glob.glob('{0}*'.format(self.cache_file)):
            os.remove(cache)

    def init_kb(self):
        if not os.path.isfile(self.kb_filename):
            with open(self.kb_filename, 'w') as fout:
                fout.write(KNOWLEDGEBASE_STUB)

    def mark_indexed(self, question_str, answer_str):
        hash_fn = os.path.join(self.kb_app_dir, get_text_hash(question_str))
        hash_contents = get_text_hash(answer_str)
        open(hash_fn, 'w').write(hash_contents)
    
    def is_indexed(self, question_str, answer_str):
        """
        Returns true if this exact combination has been previously indexed.
        Returns false otherwise.
        """
        hash_fn = os.path.join(self.kb_app_dir, get_text_hash(question_str))
        if not os.path.isfile(hash_fn):
            return False
        hash_contents = get_text_hash(answer_str)
        if open(hash_fn).read() != hash_contents:
            return False
        return True

    def add_item(self, item):
        """
        Dynamically appends an item to the knowledge base.
        Warning, this will overwrite the file!
        """
        assert isinstance(item, dict)
        assert 'questions' in item
        assert 'answers' in item and item['answers']
        for answer in item['answers']:
            assert 'date' in answer
            assert 'text' in answer
            answer.setdefault('weight', 1.0)
        item_str = yaml.dump([item], indent=4, default_flow_style=False)#, default_style='|')
        self.init_kb()
        with open(self.kb_filename, 'a') as fout:
            fout.write(item_str)

    def index_kb(self):
        es = Elasticsearch()
        count = 0
        
        if not os.path.isdir(self.kb_app_dir):
            os.mkdir(self.kb_app_dir)
            
        if self.force:
            self.delete_index()
        elif not self.is_kb_updated():
            print('No changes detected.')
            return
        
        # Count total combinations so we can accurately measure progress.
        total = 0
        self.vprint('kb_filename:', self.kb_filename)
        for item in yaml.load(open(self.kb_filename)):
            for answer in item['answers']:
                total += 1
        
        for item in yaml.load(open(self.kb_filename)):
            
            # Combine the list of separate questions into a single text block.
            questions = u'\n'.join(map(text_type, item['questions']))
            self.vprint('questions:', questions)
            
            for answer in item['answers']:
                count += 1
                sys.stdout.write('\rRe-indexing %i of %i...' % (count, total))
                sys.stdout.flush()
                
                if not self.force and self.is_indexed(questions, answer['text']):
                    continue
                
                weight = float(answer.get('weight', 1))
                dt = answer['date']
                if isinstance(dt, string_types):
                    try:
                        dt = dateutil.parser.parse(dt)
                    except ValueError as e:
                        raise Exception('Invalid date: %s' % dt)
                
                text = questions + ' ' + answer['text']
                
                _id = get_text_hash(text)
                
                doc = dict(
                    questions=questions,
                    answer=answer['text'],
                    source=answer.get('source', ''),
                    text=text,
                    action_subject=answer.get('action_subject'),
                    timestamp=dt,
                    weight=weight,
                )
                if self.verbose:
                    print('doc:')
                    pprint(doc, indent=4)
                
                # Register this combination in the database.
                # https://elasticsearch-py.readthedocs.io/en/master/api.html#elasticsearch.Elasticsearch.index
                es.index(
                    id=_id,
                    index=self.kb_index_name,
                    doc_type='text',
    #                properties=dict(
    #                    text=dict(type='string', boost=weight)
    #                ),
                    body=doc,
                )
                
                # Record a hash of this combination so we can skip it next time.
                self.mark_indexed(questions, answer['text'])
        
        self.last_reindex_count = count       
        es.indices.refresh(index=self.kb_index_name)
        self.update_kb_timestamp()
        print('\nRe-indexed %i items.' % (count,))
    
    def vprint(self, *args):
        if self.verbose:
            print(' '.join(map(str, args)))
            sys.stdout.flush()
    
    def get_local_answers(self, q=None):
        
        def _get_search_results(exact=True):
            # https://www.elastic.co/guide/en/elasticsearch/reference/current/query-dsl-query-string-query.html
            # https://www.elastic.co/guide/en/elasticsearch/reference/current/query-dsl-function-score-query.html#CO158-1
            # Order searches by a mix of how closely they match the query string
            # along with the custom weight.
            es_query = {
                "query": {
                    "function_score": {
                        "boost": '5' if exact else '1',
                        'query': {
                            'query_string':{
                                'query': query,
                                'fields': ['questions'],
                                'default_operator': 'AND' if exact else 'OR',
                            },
                        },
                        "functions": [{
                            "script_score": {
                                "script" : {
                                  "lang": "painless",
                                  "inline": "_score * doc['weight'].value"
                                },
                            },
                        }],
                    }
                }
            }

            if self.verbose:
                print('es_query:')
                pprint(es_query, indent=4)
                
            results = es.search(index=self.kb_index_name, body=es_query)
            return results
                
        query = q or self.query
        assert query and isinstance(query, string_types), 'Invalid query: %s' % query
        answers = []
        es = Elasticsearch()
        self.vprint('Checking for local answers at index %s...' % self.kb_index_name)
        es.indices.create(index=self.kb_index_name, ignore=400)

        # https://elasticsearch-py.readthedocs.io/en/master/api.html#elasticsearch.Elasticsearch.search
        #results = es.search(index=self.kb_index_name, body=es_query)
        for method in [lambda: _get_search_results(exact=True), lambda: _get_search_results(exact=False)]:
            results = method()
            total = len(results['hits']['hits'])
            self.vprint('Found %i results.' % total)
            hits = results['hits']['hits'][:self.num_answers]
            if self.verbose:
                print('results:')
                pprint(results, indent=4)
            if hits:
                for hit in hits:
                    if self.verbose:
                        print('hit:')
                        pprint(hit, indent=4)
                    answer_data = {}
                    #TODO:sort/boost by weight?
                    #TODO:ignore low weights?
                    score = hit['_score']
                    if self.min_score >= 0 and score < self.min_score:
                        continue
                    
                    answer_data['answer'] = hit['_source']['answer'].strip()
                    answer_data['score'] = score
                    answer_data['source'] = (hit['_source'].get('source') or '').strip() or None
                    answer_data['text'] = hit['_source']['text']
                    answer_data['weight'] = hit['_source']['weight']
                    answer_data['location'] = 'local'
                    if self.verbose:
                        print('answer_data:')
                        pprint(answer_data, indent=4)
                        
    #                 if self.append_header:
    #                     if answer_prefixes:
    #                         answer = '\n'.join(answer_prefixes) + '\n\n' + answer
    #                     answer = ANSWER_HEADER.format(current_position, answer)
    #                 answer = answer + '\n'
                    answers.append(answer_data)
                    
            # First try finding an entry with all the keywords using the AND operator.
            # If nothing found, then continue by searching for entries with any of the keywords
            # using the OR operator.
            if total:
                break
                        
        return answers
    
    def reindex(self, *args, **kwargs):
        return self.run_reindex(*args, **kwargs)

    def ask(self, *args, **kwargs):
        return self.run_query(*args, **kwargs)
        
    def run_query(self, q=None, output=True):
        query = q or self.query
        
        # Elasticsearch tokenizes text on certain non-alphanumeric characters,
        # so increase a queries chances of finding an exact match by removing these tokens.
        query = re.sub(r'[\:\-]+', ' ', query)
        
        answers = []
        if query:
            with fasteners.InterProcessLock(self.kb_lockfile_path):
                
                self.init_kb()
                
                # enable the cache if user doesn't want it to be disabled
                if not self.disable_cache:
                    self.enable_cache()
                
                self.append_header = self.num_answers > 1 or self.show_score or self.show_source
                #initial_position = self.pos
                
                self.vprint('Querying %s...' % query)
                
                # Check local index first.
                #http://elasticsearch.org/guide/reference/query-dsl/
                #http://www.elasticsearch.org/guide/en/elasticsearch/reference/current/query-dsl-query-string-query.html
                if not self.ignore_local:
                    answers.extend(self.get_local_answers(query))
                                
        #             except NotFoundError as e:
        #                 print('Local lookup error:', file=sys.stderr)
        #                 print(e, file=sys.stderr)
        #                 raise
                
                # If we found nothing satisfying locally, then search the net.
                if not answers and not self.ignore_remote:
                    links = self.get_links(query)
                    if not links:
                        return False
                    for answer_number in range(self.num_answers):
                        #current_position = answer_number + initial_position
#                         self.pos = current_position
                        result = self.get_answer(links)
#                         print('result:', result)
                        answer, link = result
                        if not answer and not link:
                            continue
#                         answer_prefixes = []
#                         if append_header:
#                             answer_prefixes.append('source: %s (remote)' % link)
#                             if answer_prefixes:
#                                 answer = '\n'.join(answer_prefixes) + '\n\n' + (answer or '')
#                             answer = ANSWER_HEADER.format(current_position, answer)
#                         answer = answer + '\n'
                        
                        answer_data = {}
                        answer_data['answer'] = answer
                        answer_data['score'] = 1.0
                        answer_data['source'] = link
                        answer_data['text'] = None
                        answer_data['weight'] = 1.0
                        answer_data['location'] = 'remote'
                        answers.append(answer_data)
        
        if output:
            s = []
            for i, answer in enumerate(answers):
                source_str = 'source: %s (%s)' % (answer['source'], answer['location'])
                s.append(ANSWER_HEADER.format(
                    i=i+1,
                    score=int(round(answer['score'] or 0, 0)),
                    weight=int(answer['weight'] or 0),
                    answer=answer['answer'],
                    source=source_str))
                    
#         if not args['just_check']:
#             if sys.version < '3':
#                 print(howdou(args).encode('utf-8', 'ignore'))
#             else:
#                 print(howdou(args))

#     if not args['just_check']:
#         if sys.version < '3':
#             print(howdou(args).encode('utf-8', 'ignore'))
#         else:
#             print(howdou(args))

            output_str = u'\n'+(u'\n\n'.join(s))+u'\n'        
            output_str.encode('utf-8', 'replace')
            try:
                # Try to print unicode.
                print(output_str)
            except UnicodeEncodeError:
                # If the console forces us to use ASCII, then force ASCII.
                print(output_str.encode('ascii', 'replace'))
            return output_str
                     
        return answers
        
    def run_clear_cache(self):
        self.clear_cache()
    
    def run_reindex(self):
        with fasteners.InterProcessLock(self.kb_lockfile_path):
            self.index_kb()

    def run(self):
        run_func = 'run_%s' % self.action.replace('-', '_')
        if hasattr(self, run_func):
            return getattr(self, run_func)()
        else:
            raise AttributeError('Invalid action: %s' % self.action)
         
#         if not query:
#             return ''
#         try:
#             return self.get_instructions() or 'Sorry, couldn\'t find any help with that topic.\n'
#         except (ConnectionError, SSLError):
#             return 'Failed to establish network connection.\n'


def get_parser():
    parser = argparse.ArgumentParser(description='instant coding answers via the command line')
    
    # General purpose options.
    parser.add_argument(
        '-v', '--version',
        help='Show version.',
        action='version',
        version=__version__,
    )
    parser.add_argument(
        '--verbose',
        help='If given, provides excessive output.',
        default=False,
        action='store_true')
    parser.add_argument(
        '--kb-filename',
        help='The knowledge base filename.',
        default=KNOWLEDGEBASE_FN)
    parser.add_argument(
        '--kb-index-name',
        help='The knowledge base index name to register in Elasticsearch',
        default=KNOWLEDGEBASE_INDEX)
    parser.add_argument(
        '--kb-timestamp',
        help='The filename to use to tracking timestamps.',
        default=KNOWLEDGEBASE_TIMESTAMP_FN)
    parser.add_argument(
        '--kb-app-dir',
        help='The filename to use to tracking timestamps.',
        default=APP_DATA_DIR)
    parser.add_argument(
        '--kb-lockfile-path',
        help='The filename to use when locking access during updates.',
        default=LOCKFILE_PATH)
    parser.add_argument(
        '--cache-dir',
        help='The filename to use when caching web requests.',
        default=CACHE_DIR)
    parser.add_argument(
        '--lang',
        help='The localization to use. Default is %s.' % LOCALIZATION,
        default=LOCALIZATION)

    # This controls the core behavior initiated from the command line.
    parser.add_argument(
        '--action',
        help='Action to perform. One of %s' % ('|'.join(ACTIONS)),
        default=QUERY)
    
    # Query action options.
    parser.add_argument(
        'query', metavar='QUERY', type=str, nargs='*',
        help='The question to answer or keywords to search by. Used with the query action.')
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
        default=-1, type=float)
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
    parser.add_argument(
        '--disable-cache',
        help='Disables cache of web requests.',
        default=bool(os.getenv('HOWDOU_DISABLE_CACHE')),
        action='store_true')
    
    # Reindex action options.
    parser.add_argument(
        '--force',
        help='Used with the reindex option, forces reindexing of all items even if no change was made',
        default=False,
        action='store_true')

    return parser


def command_line_runner():
    parser = get_parser()
    args = vars(parser.parse_args())
    howdou = HowDoU(**args)
    howdou.run()

if __name__ == '__main__':
    command_line_runner()
