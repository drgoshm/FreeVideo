"""Load durable operation receipts, including the legacy ref-keyed dictionaries.

New records use a list of {key, digest, result, client?}, as Jobs does, so the
same key can belong to several clients without changing the ref's syntax.
"""
from . import schema


def load(value):
    if isinstance(value, dict):
        if not all(isinstance(receipt, dict) for receipt in value.values()):
            raise ValueError('Invalid operation receipt')
        value = [dict(receipt, key=key) for key, receipt in value.items()]
    if not isinstance(value, list):
        raise ValueError('Invalid operation receipts')
    seen = set()
    for receipt in value:
        if not isinstance(receipt, dict):
            raise ValueError('Invalid operation receipt')
        schema.validate(schema.REF, receipt['key'])
        client = receipt.get('client')
        if client is not None and (not isinstance(client, str) or not 1 <= len(client) <= 80):
            raise ValueError('Invalid receipt client')
        if not isinstance(receipt['digest'], str) or not isinstance(receipt['result'], dict):
            raise ValueError('Invalid operation receipt')
        identity = (client, receipt['key'])
        if identity in seen:
            raise ValueError('Duplicate operation receipt')
        seen.add(identity)
    return value
