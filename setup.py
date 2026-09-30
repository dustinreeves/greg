#!/usr/bin/env python
from setuptools import setup

setup(
    name='Greg',
    version='0.4.8',
    install_requires=['feedparser', 'requests',
                      'windows-curses; sys_platform == "win32"'],
    extras_requires={'tagging' : ['eyeD3']},
    description='A command-line podcast aggregator',
    author='Manolo Martínez',
    author_email='manolo@austrohungaro.com',
    url='https://github.com/manolomartinez/greg',
    packages=['greg'],
    entry_points={'console_scripts': ['greg = greg.parser:main']},
    package_data={'greg': ['data/*.conf', 'data/*.html']},
    license='GPLv3'
)
