#!/usr/bin/env python

# This will generate a test suite.

def generate():
    FILES = [
        ('test/data/ShortMsgKAT_224.txt', 'sha3.SHA3224'),
        ('test/data/ShortMsgKAT_256.txt', 'sha3.SHA3256'),
        ('test/data/ShortMsgKAT_384.txt', 'sha3.SHA3384'),
        ('test/data/ShortMsgKAT_512.txt', 'sha3.SHA3512'),
        ('test/data/LongMsgKAT_224.txt', 'sha3.SHA3224'),
        ]

    print """
# This file generated by generate_tests.py

import sha3
import unittest

class SHA3Tests(unittest.TestCase):
"""

    for path, instance_str in FILES:
        contents = file(path).read().split('Len = ')
        for test in contents:
            lines = test.split('\n')
            if lines and len(lines) and not lines[0].startswith('#'):
                length = int(lines[0])
                if length % 8 == 0 and length != 0:
                    msg = lines[1].split(' = ')[-1].lower()
                    md = lines[2].split(' = ')[-1].lower()

                    print """    def test_%s_%s(self):
        inst = %s()
        inst.update(%r.decode('hex'))
        assert inst.hexdigest() == %r
""" % (path.split('/')[-1].split('.')[0], length, instance_str, msg, md)

    print """

if __name__ == '__main__':
    unittest.main()
"""

if __name__ == '__main__':
    generate()
