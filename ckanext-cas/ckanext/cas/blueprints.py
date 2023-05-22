# -*- coding: utf-8 -
from flask import Blueprint

try:
    from ckan.plugins.toolkit import redirect
except ImportError:
    from flask import redirect

from ckantoolkit import config

import datetime
import logging
import time
import urllib.request
import urllib.parse
import urllib.error
from uuid import uuid4

import ckan.lib.base as base
import ckan.lib.helpers as h
import ckan.logic as l
import ckan.model as m
import ckan.plugins as p
import ckan.plugins.toolkit as t
import requests as rq
from ckan.common import g, session

from ckan.views.user import set_repoze_user
from ckanext.cas.db import delete_entry, delete_user_entry, insert_entry
from lxml import etree, objectify

log = logging.getLogger(__name__)

render = base.render
abort = base.abort

CAS_NAMESPACE = 'urn:oasis:names:tc:SAML:2.0:protocol'
XML_NAMESPACES = {'samlp': CAS_NAMESPACE}
DATETIME_FORMAT = '%Y-%m-%dT%H:%M:%S.%fZ'


def cas_logout():
    log.debug('Invoked "cas_logout" method.')

    url = h.url_for('user.logged_out_page', _external=True)

    url_sin = h.url_for(getattr(
        t.request.environ['repoze.who.plugins']['friendlyform'],
        'logout_handler_path'),  _external=True)

    url_return = url_sin + '?came_from=' + url
    return h.redirect_to(url_return)


def _generate_saml_request(ticket_id):
    prefixes = {'SOAP-ENV': 'http://schemas.xmlsoap.org/soap/envelope/',
                'samlp': 'urn:oasis:names:tc:SAML:1.0:protocol'}

    def _generate_ns_element(prefix, element):
        return etree.QName(prefixes[prefix], element)

    for prefix, uri in list(prefixes.items()):
        etree.register_namespace(prefix, uri)

    envelope = etree.Element(_generate_ns_element('SOAP-ENV', 'Envelope'))
    etree.SubElement(envelope, _generate_ns_element('SOAP-ENV', 'Header'))
    body = etree.SubElement(
        envelope, _generate_ns_element('SOAP-ENV', 'Body'))

    request = etree.Element(_generate_ns_element('samlp', 'Request'))
    request.set('MajorVersion', '1')
    request.set('MinorVersion', '1')
    request.set('RequestID', uuid4().hex)
    request.set('IssueInstant',
                datetime.datetime.utcnow().strftime(DATETIME_FORMAT))
    artifact = etree.SubElement(
        request, _generate_ns_element('samlp', 'AssertionArtifact'))
    artifact.text = ticket_id

    body.append(request)
    return etree.tostring(envelope, encoding='UTF-8')


def cas_saml_callback(**kwargs):
    log.debug('Invoked "cas_saml_callback" method.')
    cas_plugin = p.get_plugin('cas')
    if t.request.method.lower() == 'get':
        next_url = t.request.params.get('next', '/')
        ticket = t.request.params.get(cas_plugin.TICKET_KEY)
        if not ticket:
            t.response.set_cookie(cas_plugin.LOGIN_CHECKUP_COOKIE, str(time.time()),
                                  max_age=cas_plugin.LOGIN_CHECKUP_TIME)
            return redirect(cas_plugin.CAS_APP_URL + next_url)

        log.debug('Validating ticket: {0}'.format(ticket))
        q = rq.post(cas_plugin.SAML_VALIDATION_URL + '?TARGET={0}/cas/saml_callback'.format(cas_plugin.CAS_APP_URL),
                    data=_generate_saml_request(ticket),
                    verify=cas_plugin.VERIFY_CERTIFICATE)

        root = objectify.fromstring(q.content)
        failure = False
        try:
            if root['Body']['{urn:oasis:names:tc:SAML:1.0:protocol}Response']['Status']['StatusCode'].get(
                    'Value') == 'samlp:Success':
                user_attrs = cas_plugin.USER_ATTR_MAP
                cas_attributes = [x for x in root['Body']['{urn:oasis:names:tc:SAML:1.0:protocol}Response'][
                    '{urn:oasis:names:tc:SAML:1.0:assertion}Assertion']['AttributeStatement']['Attribute']]
                data_dict = {}
                attributes = {
                    key.get('AttributeName'): key['AttributeValue'].text for key in cas_attributes}

                for key, val in list(user_attrs.items()):
                    if type(val) == list:
                        data_dict[key] = ' '.join(
                            [attributes.get(x, '') for x in val])
                    else:
                        data_dict[key] = attributes.get(val, '')

            else:
                failure = root['Body']['{urn:oasis:names:tc:SAML:1.0:protocol}Response']['Status'][
                    'StatusMessage'].text
        except AttributeError:
            failure = True

        if failure:
            # Validation failed - ABORT
            msg = 'Validation of ticket {0} failed with message: {1}'.format(
                ticket, failure)
            log.debug(msg)
            if cas_plugin.REDIRECT_ON_UNSUCCESSFUL_LOGIN:
                return redirect(cas_plugin.REDIRECT_ON_UNSUCCESSFUL_LOGIN)
            abort(401, msg)

        log.debug('Validation of ticket {0} succeeded.'.format(ticket))
        username = data_dict['user']
        email = data_dict['email']
        fullname = data_dict['fullname']
        sysadmin = data_dict['sysadmin']
        username = _authenticate_user(
            username, email, fullname, sysadmin)

        insert_entry(ticket, username)
        if 'user/login' not in next_url:
            return redirect(next_url)
        return redirect(t.h.url_for('dashboard.index', id=username))

    else:
        msg = 'MethodNotSupported: {0}'.format(t.request.method)
        log.debug(msg)
        abort(405, msg)


def _authenticate_user(username, email, fullname, is_superuser=False):
    log.debug('Invoked "_authenticate_user" method.')

    g.user = None
    g.userobj = None

    user = m.User.get(username)

    cas_plugin = p.get_plugin('cas')

    if is_superuser and is_superuser == cas_plugin.CAS_ADMIN_PROPERTY:
        is_superuser = True
    elif is_superuser and is_superuser == cas_plugin.CAS_MEMBER_PROPERTY:
        is_superuser = False
    else:
        abort(
            403, 'Solo se pueden autenticar los usuarios pertenecientes al equipo de OpenData')

    resp = h.redirect_to(u'user.me')

    if user is None:
        data_dict = {'name': str(username),
                     'email': email,
                     'fullname': fullname,
                     'password': uuid4().hex}
        try:
            user_obj = l.get_action('user_create')(
                {'ignore_auth': True}, data_dict)
        except Exception as e:
            log.error(e)
            abort(500, str(e))

        if is_superuser:
            try:
                user_obj.update({'sysadmin': True})
                l.get_action('user_update')(
                    {'ignore_auth': True}, user_obj)
            except Exception as e:
                log.error(e)
        set_repoze_user(user_obj['name'], resp)
        delete_user_entry(user_obj['name'])
        return user_obj['name']
    else:
        if user.name != username \
                or user.email != email \
                or user.fullname != fullname \
                or user.sysadmin != is_superuser:
            try:
                user_obj = {'id': user.id,
                            'email': email,
                            'fullname': fullname,
                            'sysadmin': is_superuser}
                l.get_action('user_update')(
                    {'ignore_auth': True}, user_obj)
            except Exception as e:
                log.error(e)
                abort(500, str(e))

        set_repoze_user(user.name, resp)
        delete_user_entry(user.name)
        session['user'] = user.name
        return session.get('user')


def cas_callback(**kwargs):
    log.debug('Invoked "cas_callback" method.')
    cas_plugin = p.get_plugin('cas')
    if t.request.method.lower() == 'get':
        next_url = t.request.params.get('next', cas_plugin.CAS_APP_URL)
        ticket = t.request.params.get(cas_plugin.TICKET_KEY)
        if not ticket:
            t.response.set_cookie(cas_plugin.LOGIN_CHECKUP_COOKIE, str(time.time()),
                                  max_age=cas_plugin.LOGIN_CHECKUP_TIME)
            log.debug('(NOT TICKET) REDIRECTING TO ' +
                      cas_plugin.CAS_APP_URL)
            return redirect(cas_plugin.CAS_APP_URL)

        # log.debug('Validating ticket: {0}'.format(ticket))
        q = rq.get(cas_plugin.SERVICE_VALIDATION_URL,
                   params={cas_plugin.TICKET_KEY: ticket,
                           cas_plugin.SERVICE_KEY: cas_plugin.CAS_APP_URL + '/cas/callback'}, verify=cas_plugin.VERIFY_CERTIFICATE)
        log.debug(b'' + q.content)
        root = objectify.fromstring(b'' + q.content)
        isMemberOf = None
        if hasattr(root.authenticationSuccess.attributes, 'isMemberOf'):
            log.debug('Cantidad de isMemberOf :')
            log.debug(
                len(list(root.authenticationSuccess.attributes.isMemberOf)))
            log.debug(list(root.authenticationSuccess.attributes.isMemberOf))
            isMemberOfList = list(
                root.authenticationSuccess.attributes.isMemberOf)
            for attr in isMemberOfList:
                log.debug(attr)
                if (attr.text.find(cas_plugin.CAS_BASE_PROPERTY) != -1):
                    isMemberOf = attr
                    print(('Set up isMemberOf', isMemberOf))
                    log.debug('Valor de isMemberOf :')
                    log.debug(isMemberOf)
        log.debug(type(b'' + q.content))
        try:
            if hasattr(root.authenticationSuccess, 'user'):
                username = root.authenticationSuccess.user
                success = True
        except AttributeError:
            success, username = False, None

        try:
            failure = root.authenticationFailure
        except AttributeError:
            failure = False

        if failure:
            # Validation failed - ABORT
            msg = 'Validation of ticket {0} failed with message: {1}'.format(
                ticket, failure)
            log.debug(msg)
            if cas_plugin.REDIRECT_ON_UNSUCCESSFUL_LOGIN:
                log.debug('(UNSUCCESSFUL LOGIN) REDIRECTING TO ' +
                          cas_plugin.REDIRECT_ON_UNSUCCESSFUL_LOGIN)
                return redirect(cas_plugin.REDIRECT_ON_UNSUCCESSFUL_LOGIN)
            abort(401, msg)

        log.debug('Validation of ticket {0} succedded. Authenticated user: {1}'.format(
            ticket, username.text))
        attrs = root.authenticationSuccess.attributes.__dict__

        if isMemberOf is not None:
            attrs.update(isMemberOf=isMemberOf)
        log.debug('Validated with attrs')
        log.debug(attrs)
        data_dict = {}
        for key, val in list(cas_plugin.USER_ATTR_MAP.items()):
            if type(val) == list:
                data_dict[key] = ' '.join([attrs.get(x).text for x in val])
            else:
                log.debug(
                    '-->Obteniendo el parámetro [{0}] con clave [{1}]'.format(key, val))
                if attrs.get(val) is not None:
                    data_dict[key] = attrs.get(val).text

        fullname = data_dict['fullname']
        email = data_dict['email']
        username = str(root.authenticationSuccess.user)
        sysadmin = False

        sysadmin_check = data_dict.get('sysadmin')
        if sysadmin_check and sysadmin_check.strip():
            cn = data_dict['sysadmin'].partition(
                "cn=")[2].partition(",o=")[0]
            sysadmin = cn
            log.debug('CN filtrado :')
            log.debug(sysadmin)
        else:
            abort(
                403, 'Solo se pueden autenticar los usuarios pertenecientes al equipo de OpenData')

        username = _authenticate_user(
            username, email, fullname, sysadmin)
        insert_entry(ticket, username)
        if 'user/login' not in next_url:
            log.debug('(NEXT_URL) REDIRECTING TO ' + next_url)
            return redirect(next_url)
        log.debug('USERNAME :' + username)
        log.debug('(END CAS/CALLBACK) REDIRECTING TO ' +
                  t.url_for('dashboard.index', id=username, _external=True))
        return redirect(t.url_for('dashboard.index', id=username, _external=True))

    else:
        msg = 'MethodNotSupported: {0}'.format(t.request.method)
        log.debug(msg)
        abort(405, msg)


cas = Blueprint('cas', __name__)


rules = [
    ('/cas/callback', 'cas_callback', cas_callback),
    ('/cas/saml_callback', 'cas_saml_callback', cas_saml_callback),
    ('/cas/logout', 'cas_logout', cas_logout),
]


def register_url_redirect():
    return redirect(config.get('ckanext.cas.register_url'))


for rule in rules:
    cas.add_url_rule(*rule)

if config.get('ckanext.cas.register_url', None):
    cas.add_url_rule('/user/register',
                     'register_url_redirect', register_url_redirect)
