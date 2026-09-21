"""Services: the MCP's own business logic.

A service owns authorization, the actor envelope, feature-flag routing, and the
translation of a repository failure into the ``ToolError`` string the tool
descriptions teach the model to act on. Tools own registration, argument
models, and the docstrings that are the agent-facing contract — nothing else.
"""
