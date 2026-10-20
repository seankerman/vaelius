"""Operator allowlist for model providers; nothing is selected implicitly.

`runtime.json` names the providers, and optionally the model IDs and regions, that
any enabled model stage may use. A stage must name its provider and model itself.
An absent `models` section allows no provider, so enabling a stage without an
allowlist fails closed at startup instead of reaching an external service.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

PROVIDERS = frozenset({'bedrock', 'operator_api', 'codex'})
_REGION = re.compile(r'[a-z]{2}(-[a-z]+)+-[0-9]{1,2}')


class ModelPolicyError(ValueError):
    """Content-free configuration refusal raised before any provider dispatch."""


@dataclass(frozen=True)
class ModelPolicy:
    providers: frozenset = frozenset()
    models: frozenset | None = None
    regions: frozenset | None = None


def _names(value, field, *, check=None):
    if (not isinstance(value, list) or len(value) > 64
            or any(not isinstance(v, str) or not v or len(v) > 2048 or v != v.strip() for v in value)
            or len(set(value)) != len(value)):
        raise ModelPolicyError('runtime_models_configuration: '+field+' must be a list of unique names')
    if check is not None:
        unknown = sorted(v for v in value if not check(v))
        if unknown:
            raise ModelPolicyError('runtime_models_configuration: unsupported '+field+' '+', '.join(unknown))
    return frozenset(value)


def policy_from_settings(settings):
    """Parse `runtime.json` `models`; unknown keys and provider names are refused."""
    models = settings.get('models', {})
    if not isinstance(models, dict) or set(models) - {'allowed_providers', 'allowed_models', 'allowed_regions'}:
        raise ModelPolicyError('runtime_models_configuration: allowed keys are '
                               'allowed_providers, allowed_models and allowed_regions')
    providers = _names(models.get('allowed_providers', []), 'allowed_providers',
                       check=PROVIDERS.__contains__)
    selected = {}
    if 'allowed_models' in models:
        selected['models'] = _names(models['allowed_models'], 'allowed_models')
    if 'allowed_regions' in models:
        selected['regions'] = _names(models['allowed_regions'], 'allowed_regions',
                                     check=lambda v: bool(_REGION.fullmatch(v)))
    return ModelPolicy(providers, **selected)


def stage_selection(config, stage):
    """Return the explicit (provider, model, region) a live stage would dispatch to.

    The Codex adapter has always read its model from `observer.model`; API-backed
    adapters read `backend_execution.model`. Neither has a fallback model.
    """
    selected = config.get('backend_execution') if isinstance(config, dict) else None
    if not isinstance(selected, dict) or not isinstance(selected.get('kind'), str) or not selected['kind']:
        raise ModelPolicyError(f'model_stage_provider_required: stage {stage} must set backend_execution.kind')
    provider = selected['kind']
    if provider == 'codex':
        observer = config.get('observer')
        model = observer.get('model') if isinstance(observer, dict) else None
        field = 'observer.model'
    else:
        model, field = selected.get('model'), 'backend_execution.model'
    if not isinstance(model, str) or not model.strip():
        raise ModelPolicyError(f'model_stage_model_required: stage {stage} provider {provider} must set {field}')
    region = selected.get('region') if provider == 'bedrock' else None
    return provider, model, region


def require_stage_allowed(policy, config, stage):
    """Refuse a live stage unless its provider, model and region are allowlisted."""
    if not isinstance(policy, ModelPolicy):
        policy = ModelPolicy()
    provider, model, region = stage_selection(config, stage)
    if provider not in policy.providers:
        raise ModelPolicyError(f'model_provider_not_allowlisted: stage {stage} provider {provider} is not in '
                               'runtime.json models.allowed_providers')
    if policy.models is not None and model not in policy.models:
        raise ModelPolicyError(f'model_not_allowlisted: stage {stage} model {model} is not in '
                               'runtime.json models.allowed_models')
    if provider == 'bedrock':
        if not isinstance(region, str) or not _REGION.fullmatch(region):
            raise ModelPolicyError(f'model_stage_region_required: stage {stage} provider bedrock must set '
                                   'backend_execution.region')
        if policy.regions is not None and region not in policy.regions:
            raise ModelPolicyError(f'model_region_not_allowlisted: stage {stage} region {region} is not in '
                                   'runtime.json models.allowed_regions')
    return provider, model, region
