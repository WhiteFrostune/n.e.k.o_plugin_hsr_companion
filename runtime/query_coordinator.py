"""Compatibility entrypoint for relocated query coordination APIs."""

from .contracts import QueryObservation as QueryObservation
from .query_observation import resolve_query as resolve_query
from .query_status import answer_status as answer_status
from .query_status import needs_followup as needs_followup
from .readiness import FieldReadiness as FieldReadiness
from .readiness import answer_coverage as answer_coverage
from .readiness import evaluate_readiness as evaluate_readiness
from .readiness import has_answerable_content as has_answerable_content
from .readiness import navigation_applicable as navigation_applicable
from .readiness import satisfied_fields as satisfied_fields
