import setuptools

setuptools.setup(
    name="wott-agent",
    version='0.1.0',

    author="Viktor Petersson",
    author_email="v@viktopia.io",

    description="WoTT agent",

    packages=setuptools.find_packages('src', exclude=('tests',)),
    package_dir={'': 'src'},
    include_package_data=True,
    zip_safe=False,

    install_requires=[
        'certifi',
        'cffi',
        'chardet',
        'cryptography',
        'idna',
        'pyOpenSSL',
        'requests'
    ],

    classifiers=[
        'Development Status :: 2 - Pre-Alpha',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.5',
        'Topic :: Internet :: WWW/HTTP',
        'Topic :: Security',
        'Topic :: System :: Installation/Setup',
        'Topic :: System :: Networking',
        'Topic :: System :: Systems Administration',
        'Topic :: Utilities',
    ],

    entry_points={
        'console_scripts': [
            'wott-agent = agent.main:main',
        ],
    }
)
