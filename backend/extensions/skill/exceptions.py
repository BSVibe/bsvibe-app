"""Skill-domain exception hierarchy."""


class SkillError(Exception):
    """Base exception for all skill-domain errors."""


class SkillLoadError(SkillError):
    """Raised when a skill .md fails to parse / validate."""


class SkillRunError(SkillError):
    """Raised when ``invoke_skill`` fails during execution."""
