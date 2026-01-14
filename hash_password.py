#!/usr/bin/env python3
"""Simple script to hash a password"""
import sys
import bcrypt

if len(sys.argv) < 2:
    print("Usage: python3 hash_password.py <password>", file=sys.stderr)
    sys.exit(1)

password = sys.argv[1].encode('utf-8')

# Hash using bcrypt directly
hashed = bcrypt.hashpw(password, bcrypt.gensalt()).decode('utf-8')
print(hashed)

