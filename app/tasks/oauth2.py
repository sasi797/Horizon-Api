import msal

_msal_app: msal.ConfidentialClientApplication | None = None
_msal_settings_key: tuple | None = None


def get_graph_token(settings) -> str:
    global _msal_app, _msal_settings_key
    key = (settings.AZURE_CLIENT_ID, settings.AZURE_TENANT_ID)
    if _msal_app is None or _msal_settings_key != key:
        _msal_app = msal.ConfidentialClientApplication(
            client_id=settings.AZURE_CLIENT_ID,
            client_credential=settings.AZURE_CLIENT_SECRET,
            authority=f"https://login.microsoftonline.com/{settings.AZURE_TENANT_ID}",
        )
        _msal_settings_key = key
    result = _msal_app.acquire_token_for_client(
        scopes=["https://graph.microsoft.com/.default"]
    )
    if "access_token" not in result:
        raise Exception(f"Graph token error: {result.get('error_description', result)}")
    return result["access_token"]
