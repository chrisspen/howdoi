#!/usr/bin/env python

from setuptools import setup, find_packages, Command
import os
import sys

import howdou

try:
    from pypandoc import convert
    read_md = lambda f: convert(f, 'rst')
except ImportError:
    print("warning: pypandoc module not found, could not convert Markdown to RST")
    read_md = lambda f: open(f, 'r').read()

def get_reqs():
    ret = [
        _.strip()
        for _ in open('requirements.txt', 'r').readlines()
        if _.strip()
    ]
    if sys.version_info < (2,7):
        ret.append('argparse')
    return ret

class TestCommand(Command):
    description = "Runs unittests."
    user_options = [
        ('name=', None,
         'Name of the specific test to run.'),
        ('virtual-env-dir=', None,
         'The location of the virtual environment to use.'),
        ('pv=', None,
         'The version of Python to use. e.g. 2.7, 3.2, etc.'),
    ]
    
    def initialize_options(self):
        self.name = None
        self.virtual_env_dir = './.env%s'
        self.pv = 0
        self.versions = [2.7, 3.2]
        
    def finalize_options(self):
        pass
    
    def build_virtualenv(self, pv):
        virtual_env_dir = self.virtual_env_dir % pv
        kwargs = dict(virtual_env_dir=virtual_env_dir, pv=pv)
        if not os.path.isdir(virtual_env_dir):
            cmd = 'virtualenv -p /usr/bin/python{pv} {virtual_env_dir}'.format(**kwargs)
            print(cmd)
            os.system(cmd)
            
            cmd = '{virtual_env_dir}/bin/easy_install -U distribute'.format(**kwargs)
            print(cmd)
            os.system(cmd)
            
            for package in get_reqs():
                kwargs['package'] = package
                cmd = '{virtual_env_dir}/bin/pip install -U {package}'.format(**kwargs)
                print(cmd)
                os.system(cmd)
    
    def run(self):
        versions = self.versions
        if self.pv:
            versions = [self.pv]
        
        for pv in versions:
            
            self.build_virtualenv(pv)
            kwargs = dict(pv=pv, name=self.name)
                
            if self.name:
                cmd = './.env{pv}/bin/nosetests howdou.tests:HowdouTestCase.{name}'.format(**kwargs)
            else:
                cmd = './.env{pv}/bin/nosetests'.format(**kwargs)
                
            print(cmd)
            ret = os.system(cmd)
            if ret:
                return

setup(
    name='howdou',
    version=howdou.__version__,
    description='Instant coding answers via the command line',
    long_description=read_md('README.md'),
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Environment :: Console",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 2",
        "Programming Language :: Python :: 2.6",
        "Programming Language :: Python :: 2.7",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.2",
        "Programming Language :: Python :: 3.3",
        "Topic :: Documentation",
    ],
    keywords='howdou help console command line answer',
    author='Chris Spencer',
    author_email='chrisspen@gmail.com',
    url='https://github.com/chrisspen/howdou',
    license='MIT',
    packages=find_packages(),
    entry_points={
        'console_scripts': [
            'howdou = howdou.howdou:command_line_runner',
        ]
    },
    cmdclass={
        'test': TestCommand,
    },
    install_requires=get_reqs(),
)
