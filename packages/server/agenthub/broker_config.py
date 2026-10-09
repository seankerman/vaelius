from agenthub.cloud_identity import OIDCBroker

def broker_from_settings(settings):
    if settings.get('kind')=='oidc':
        return OIDCBroker(**{k:settings[k] for k in ('issuer','client_id','authorization_endpoint',
            'token_endpoint','jwks_uri','redirect_uri')})
    if settings.get('kind','keycloak')!='keycloak':raise ValueError('unsupported_identity_broker')
    issuer=settings['base_url']+'/realms/'+settings['realm']
    return OIDCBroker(issuer=issuer,client_id=settings['client_id'],
        authorization_endpoint=issuer+'/protocol/openid-connect/auth',
        token_endpoint=issuer+'/protocol/openid-connect/token',jwks_uri=issuer+'/protocol/openid-connect/certs',
        redirect_uri=settings['redirect_uri'])
