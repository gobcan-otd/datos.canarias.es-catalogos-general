# -*- coding: utf-8 -
from ckantoolkit import config

from flask import redirect

import logging
import re
import urllib.request
import urllib.parse
import urllib.error
import ckan.lib.base as base
import ckan.lib.helpers as h
import ckan.plugins as p
import ckan.plugins.toolkit as t
import ckanext.cas.blueprints as blueprints
from ckanext.cas.db import setup as db_setup
from ckanext.cas.db import delete_user_entry, is_ticket_valid
from ckan.common import g, session
import ckan.model as model


render = base.render
abort = base.abort


log = logging.getLogger(__name__)


class CASClientPlugin(p.SingletonPlugin):
    p.implements(p.IConfigurer)
    p.implements(p.IAuthenticator, inherit=True)
    p.implements(p.IBlueprint)
    p.implements(p.IConfigurable)

    USER_ATTR_MAP = {}
    TICKET_KEY = None
    SERVICE_KEY = None
    SERVICE_VALIDATION_URL = None
    SAML_VALIDATION_URL = None
    CAS_LOGOUT_URL = None
    CAS_LOGIN_URL = None
    CAS_COOKIE_NAME = None
    CAS_VERSION = None
    CAS_APP_URL = None
    REDIRECT_ON_UNSUCCESSFUL_LOGIN = None
    VERIFY_CERTIFICATE = True
    CAS_ROOT_PATH = None
    CAS_ADMIN_PROPERTY = None
    CAS_MEMBER_PROPERTY = None
    CAS_BASE_PROPERTY = None

    def _generate_login_url(self, gateway=False, next=False):
        params = '?service='
        if gateway:
            params = '?gateway=true&service='
        if self.CAS_VERSION == 2:
            url = self.CAS_LOGIN_URL + params + self.CAS_APP_URL + '/cas/callback'
        # Esto se pone para evitar redirecciones infinitas al loguearse.
        # Antes estaba comentada la condicion next
        # pero eso no permitia que al entrar por primera vez se guardase
        # la ruta completa y te redirigia a la página principal.
            log.debug('CURRENT URL: ' +
                      urllib.parse.quote(t.request.environ['CKAN_CURRENT_URL']))
            current = urllib.parse.quote(t.request.environ['CKAN_CURRENT_URL'])
            if '/user/login' in current or '/cas/callback' in current:
                next = False
        elif self.CAS_VERSION == 3:
            url = self.CAS_LOGIN_URL + params + self.CAS_APP_URL + '/cas/saml_callback'
        if next:
            url = url + '?next=' + \
                urllib.parse.quote(t.request.environ['CKAN_CURRENT_URL'])
        log.debug('RETURNING URL:' + url)
        return url

    # IConfigurable

    def configure(self, config_):
        # Setup database tables
        db_setup()

        # Load and parse user attributes mapping
        user_mapping = t.aslist(config_.get('ckanext.cas.user_mapping'))
        for attr in user_mapping:
            key, val = attr.split('~')
            if '+' in val:
                val = val.split('+')
            self.USER_ATTR_MAP.update({key: val})

        if not any(self.USER_ATTR_MAP):
            raise RuntimeError(
                'User attribute mapping is required for plugin: {0}'.format(self.name))

        if 'email' not in list(self.USER_ATTR_MAP.keys()):
            raise RuntimeError(
                '"email" attribute mapping is required for plugin: {0}'.format(self.name))

        service_validation_url = config.get(
            'ckanext.cas.service_validation_url', None)
        saml_validation_url = config.get(
            'ckanext.cas.saml_validation_url', None)

        if (service_validation_url and saml_validation_url) or \
                (not service_validation_url and not saml_validation_url):
            msg = 'Configure either "ckanext.cas.service_validation_url" or "ckanext.cas.saml_validation_url" but not both.'
            raise RuntimeError(msg)

        if not config.get('ckanext.cas.login_url', None):
            raise RuntimeError(
                '"ckanext.cas.login_url" is required for plugin: {0}'.format(self.name))

        if not config.get('ckanext.cas.logout_url', None):
            raise RuntimeError(
                '"ckanext.cas.logout_url" is required for plugin: {0}'.format(self.name))

        if not config.get('ckanext.cas.application_url', None):
            raise RuntimeError(
                '"ckanext.cas.application_url" is required for plugin: {0}'.format(self.name))

        if not config.get('ckanext.cas.admin_property', None):
            raise RuntimeError(
                '"ckanext.cas.admin_property" is required for plugin: {0}'.format(self.name))

        if not config.get('ckanext.cas.member_property', None):
            raise RuntimeError(
                '"ckanext.cas.member_property" is required for plugin: {0}'.format(self.name))

        if not config.get('ckanext.cas.base_property', None):
            raise RuntimeError(
                '"ckanext.cas.base_property" is required for plugin: {0}'.format(self.name))

        if service_validation_url:
            self.SERVICE_VALIDATION_URL = config.get(
                'ckanext.cas.service_validation_url')
            self.CAS_VERSION = 2
        elif saml_validation_url:
            self.SAML_VALIDATION_URL = config.get(
                'ckanext.cas.saml_validation_url')
            self.CAS_VERSION = 3

        self.CAS_LOGIN_URL = config.get('ckanext.cas.login_url')
        self.CAS_LOGOUT_URL = config.get('ckanext.cas.logout_url')
        self.CAS_APP_URL = config.get('ckanext.cas.application_url')
        self.CAS_COOKIE_NAME = config.get(
            'ckanext.cas.cookie_name', 'sessionid')
        self.TICKET_KEY = config.get('ckanext.cas.ticket_key', 'ticket')
        self.SERVICE_KEY = config.get('ckanext.cas.service_key', 'service')
        self.REDIRECT_ON_UNSUCCESSFUL_LOGIN = config.get(
            'ckanext.cas.unsuccessful_login_redirect_url', None)
        self.LOGIN_CHECKUP_TIME = t.asint(
            config.get('ckanext.cas.login_checkup_time', 600))
        self.LOGIN_CHECKUP_COOKIE = config.get(
            'ckanext.cas.login_checkup_cookie', 'cas_login_check')
        self.VERIFY_CERTIFICATE = t.asbool(
            config.get('ckanext.cas.verify_certificate', True))
        self.CAS_ROOT_PATH = config.get('ckanext.cas.root_path', None)
        self.CAS_ADMIN_PROPERTY = config.get(
            'ckanext.cas.admin_property', None)
        self.CAS_MEMBER_PROPERTY = config.get(
            'ckanext.cas.member_property', None)
        self.CAS_BASE_PROPERTY = config.get(
            'ckanext.cas.base_property', None)

    # IConfigurer

    def update_config(self, config_):
        t.add_template_directory(config_, 'templates')
        t.add_public_directory(config_, 'public')
        t.add_resource('fanstatic', 'cas')

    # IAuthenticator

    def identify(self):
        log.debug('Invoked "identify" method.')

        environ = t.request.environ

        remote_user = session.get('user')

        g.user = remote_user
        g.userobj = model.User.get(remote_user)

        log.debug('IS TICKET VALID ?')
        log.debug(is_ticket_valid(remote_user))
        if remote_user and not is_ticket_valid(remote_user):
            log.debug('User logged out of CAS Server')

            if(self.CAS_ROOT_PATH is not None):
                url = h.url_for('user.logged_out_page')
                logout_path = getattr(
                    t.request.environ['repoze.who.plugins']['friendlyform'],
                    'logout_handler_path')
                return h.redirect_to(
                    '{root_path}{logout_path}?came_from={url}'
                    .format(
                        root_path=self.CAS_ROOT_PATH,
                        logout_path=logout_path,
                        url=url
                    ))

            url = h.url_for('user.logged_out_page')
            return h.redirect_to(
                getattr(
                    t.request.environ['repoze.who.plugins']['friendlyform'],
                    'logout_handler_path'
                ) + '?came_from=' + url)

        elif not remote_user \
                and not re.match(r'.*/api(/\d+)?/action/.*', environ['PATH_INFO']) \
                and not re.match(r'.*/dataset/.+/resource/.+/download/.+', environ['PATH_INFO']):
            login_checkup_cookie = t.request.cookies.get(
                self.LOGIN_CHECKUP_COOKIE, None)
            if login_checkup_cookie:
                return
            # log.debug('Checking if CAS session exists for user')
            # url = self._generate_login_url(gateway=True, next=True)
            # redirect(url)

    def login(self):
        log.debug('Invoked "login" method.')

        cas_login_url = self._generate_login_url(next=True)
        return redirect(cas_login_url)

    def logout(self):
        log.debug('Invoked "logout" method.')

        remote_user = session.get('user')

        if remote_user:
            keys_to_delete = [
                key for key in session if key.startswith('user')
            ]
        if keys_to_delete:
            for key in keys_to_delete:
                del session[key]
                g.user = None
                g.userobj = None
            session.save()
        delete_user_entry(t.c.user)
        if t.asbool(config.get('ckanext.cas.single_sign_out')):
            cas_logout_url = self.CAS_LOGOUT_URL + \
                '?service=' + self.CAS_APP_URL + '/cas/logout'
            return redirect(cas_logout_url)
        # TODO: Refactor into helper
        url = h.url_for('user.logged_out_page')
        return h.redirect_to(
            getattr(
                t.request.environ['repoze.who.plugins']['friendlyform'],
                'logout_handler_path') + '?came_from=' + url)

    # IBlueprint

    def get_blueprint(self):
        return [blueprints.cas]
