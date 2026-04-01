"""Project context package — builds the agent's mental model of the codebase.

Three responsibilities, one per module:
  manifest.py  — detect project type + key metadata from fingerprint files
  filetree.py  — walk the directory tree respecting ignore rules
  project.py   — assemble everything into ProjectContext + build_system_prompt block
"""

from .project import ProjectContext, build_project_context

__all__ = ["ProjectContext", "build_project_context"]
