"""Visa-sponsorship signal — fuzzy-match companies against gov sponsor registries,
with JD-keyword and Gulf-automatic fallbacks. See classifier.classify()."""

from .classifier import classify
from .registries import available, load, norm_company

__all__ = ["classify", "available", "load", "norm_company"]
