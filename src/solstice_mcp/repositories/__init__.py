"""Repositories: typed access to the stores this server reads and writes.

One subpackage per backing store. A repository knows transport, paths, and
payload shapes; it never knows the tool vocabulary the agent is instructed
against. Translating a failure into what an agent should do about it belongs to
the service above it.
"""
