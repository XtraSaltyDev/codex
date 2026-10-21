"""Expose only model controls advertised by the native Codex catalog."""
ACCESS = {
    'full': {'approvalPolicy': 'never', 'sandboxPolicy': {'type': 'dangerFullAccess'}},
    'workspace': {'approvalPolicy': 'on-request', 'sandboxPolicy': {'type': 'workspaceWrite', 'networkAccess': True,
                  'excludeTmpdirEnvVar': False, 'excludeSlashTmp': False}},
    'readOnly': {'approvalPolicy': 'on-request', 'sandboxPolicy': {'type': 'readOnly'}},
}


def selection(catalog, defaults, saved):
    model_id = saved.get('modelId') or defaults.get('modelId') or ''
    model = catalog.get(model_id, {})
    efforts = model.get('supportedReasoningEfforts', [])
    tiers = model.get('serviceTiers', [])
    return {'providerId': defaults.get('providerId', 'spark'), 'modelId': model_id,
            'status': 'available' if model_id in catalog else 'unavailable', 'models': list(catalog),
            'modelNames': {key: value.get('displayName', key) for key, value in catalog.items()},
            'reasoningEffort': saved.get('reasoningEffort') or defaults.get('reasoningEffort') or model.get('defaultReasoningEffort'),
            'reasoningOptions': [{'id': option['reasoningEffort'], 'title': {'none': 'None', 'xhigh': 'Extra high'}.get(option['reasoningEffort'], option['reasoningEffort'].title()),
                                  'description': option.get('description', '')} for option in efforts],
            'serviceTier': saved.get('serviceTier') or 'default',
            'speedOptions': ([{'id': 'default', 'title': 'Standard', 'description': 'Use the host’s default service tier.'}] +
                             [{'id': tier['id'], 'title': tier['name'], 'description': tier.get('description', '')} for tier in tiers if tier['id'] != 'default']) if tiers else []}


def validate(catalog, defaults, saved, updates):
    if not isinstance(updates, dict) or set(updates) - {'model', 'reasoning', 'access', 'speed'} or not all(isinstance(v, str) for v in updates.values()):
        raise ValueError('Unsupported conversation setting.')
    result = dict(saved)
    if 'model' in updates:
        if updates['model'] not in catalog:
            raise ValueError('This model is no longer available on the host.')
        previous = saved.get('modelId') or defaults.get('modelId')
        result['modelId'] = updates['model']
        if updates['model'] != previous:
            result['reasoningEffort'] = catalog[updates['model']].get('defaultReasoningEffort')
            result['serviceTier'] = None
    choices = selection(catalog, defaults, result)
    if 'reasoning' in updates:
        if updates['reasoning'] not in {item['id'] for item in choices['reasoningOptions']}:
            raise ValueError('This reasoning level is not supported by the selected model.')
        result['reasoningEffort'] = updates['reasoning']
    if 'speed' in updates:
        if updates['speed'] not in {item['id'] for item in choices['speedOptions']}:
            raise ValueError('This speed is not supported by the selected model.')
        result['serviceTier'] = None if updates['speed'] == 'default' else updates['speed']
    if 'access' in updates:
        if updates['access'] not in ACCESS:
            raise ValueError('Unsupported access setting.')
        result['access'] = updates['access']
        result['fullAccess'] = updates['access'] == 'full'
    return result
