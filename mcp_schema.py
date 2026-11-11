"""Shared JSON schema values, independent of mailbox or chat implementations."""

TEXT = {"type": "string", "minLength": 1, "maxLength": 100000}
ROOM = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$", "maxLength": 48}


def schema(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}
