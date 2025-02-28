# (C) Datadog, Inc. 2019-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from codecs import open  # To use a consistent encoding
from os import path

from setuptools import setup

HERE = path.abspath(path.dirname(__file__))

ABOUT = {}
with open(path.join(HERE, "datadog_checks", "downloader", "__about__.py")) as f:
    exec(f.read(), ABOUT)

# Get the long description from the README file
LONG_DESC = ""
with open(path.join(HERE, 'README.md'), encoding='utf-8') as f:
    LONG_DESC = f.read()


def get_dependencies():
    dep_file = path.join(HERE, 'requirements.in')
    if not path.isfile(dep_file):
        return []

    with open(dep_file, encoding='utf-8') as f:
        return f.readlines()


def parse_pyproject_array(name):
    import os
    import re
    from ast import literal_eval

    pattern = r'^{} = (\[.+?\])$'.format(name)

    with open(os.path.join(HERE, 'pyproject.toml'), 'r', encoding='utf-8') as f:
        # Windows \r\n prevents match
        contents = '\n'.join(line.rstrip() for line in f.readlines())

    array = re.search(pattern, contents, flags=re.MULTILINE | re.DOTALL).group(1)
    return literal_eval(array)


setup(
    # Version should always match one from an agent release
    version=ABOUT["__version__"],
    name='datadog_checks_downloader',
    description='The Datadog Checks Downloader',
    long_description=LONG_DESC,
    long_description_content_type='text/markdown',
    keywords='datadog agent checks',
    url='https://github.com/DataDog/integrations-core',
    author='Datadog',
    author_email='packages@datadoghq.com',
    license='BSD',
    # See https://pypi.org/classifiers
    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'Intended Audience :: Developers',
        'Topic :: System :: Monitoring',
        'License :: OSI Approved :: BSD License',
        'Programming Language :: Python :: 2',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.8',
    ],
    packages=['datadog_checks.downloader'],
    # Run-time dependencies
    extras_require={'deps': parse_pyproject_array('deps')},
    # NOTE: Copy over TUF directories, and root metadata.
    package_data={
        'datadog_checks.downloader': [
            'data/repo/metadata/current/root.json',
            # To coax both git and Python to track initially empty directories.
            'data/repo/metadata/previous/.gitignore',
            'data/repo/targets/.gitignore',
        ]
    },
    include_package_data=True,
)
