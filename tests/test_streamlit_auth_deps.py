"""Guards the Streamlit OIDC dependencies that the UI login flow relies on."""

from streamlit import auth_util


def test_authlib_is_installed_for_streamlit_login():
    # st.login() needs Authlib >= 1.3.2, which the streamlit[auth] extra provides.
    assert auth_util.is_authlib_installed()


def test_streamlit_can_expose_access_tokens():
    # The UIs read the API access token from st.user.tokens["access"], which
    # requires `expose_tokens` support (Streamlit >= 1.53).
    assert callable(auth_util.get_expose_tokens_config)
