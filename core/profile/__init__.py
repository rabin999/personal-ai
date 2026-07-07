"""Config & User Profile module (spec §2)."""

from core.profile.models import (
    AudioPrefs,
    CommPrefs,
    ModelPrefs,
    ProjectType,
    TraitDef,
    UserProfile,
)
from core.profile.service import ProfileNotFound, ProfileService, TraitRegistry

__all__ = [
    "AudioPrefs",
    "CommPrefs",
    "ModelPrefs",
    "ProfileNotFound",
    "ProfileService",
    "ProjectType",
    "TraitDef",
    "TraitRegistry",
    "UserProfile",
]
