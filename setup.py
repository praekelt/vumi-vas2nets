from setuptools import setup, find_packages

setup(
    name="vxvas2nets",
    version="0.7.0",
    url='http://github.com/praekelt/vumi-vas2nets',
    license='BSD',
    description="A Vas2Nets USSD transport for Vumi.",
    long_description=open('README.rst', 'r').read(),
    author='Praekelt Foundation',
    author_email='dev@praekeltfoundation.org',
    packages=find_packages(),
    include_package_data=True,
    install_requires=[
        'vumi>=0.6.0',
        'treq',
    ],
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: BSD License',
        'Operating System :: POSIX',
        'Programming Language :: Python',
        'Topic :: Software Development :: Libraries :: Python Modules',
        'Topic :: System :: Networking',
    ],
)
