import codecs
import json
import os

import ckan.lib.base as base
import ckan.lib.helpers as h
import ckan.plugins as plugins
import ckan.plugins.toolkit as toolkit
from ckan.common import _, request
from ckan.lib.plugins import DefaultTranslation
from ckantoolkit import config
from flask import Blueprint, redirect

HERE = os.path.abspath(os.path.dirname(__file__))
I18N_DIR = os.path.join(HERE, "../i18n")

dirname = os.path.dirname(__file__)
CONFIG_FILE_PATH = os.path.join(dirname, '../etc/config.json')

abort = base.abort


def dataset_redirect():
    return redirect('dataset')


def abort_redirect():
    abort(403, _(u'Not authorized to see this page'))


def get_group_pagination_limit():
    """
    Devuelve el limite de paginacion para grupos definida en el archivo
    de configuracion etc/config.json
    """

    with codecs.open(CONFIG_FILE_PATH, 'r') as json_file:
        json_data = json.loads(json_file.read())
        # Se asume que es un numero entero, en otro caso habra que hacer cast con int()
        return json_data['metadata']['groupPaginationLimit']


def get_all_groups():
    '''Return a list of all the groups
    '''
    items_per_page = get_group_pagination_limit()
    page = h.get_page_number(request.params) or 1
    data_dict = {
        'all_fields': True,
        'limit': items_per_page,
        'offset': items_per_page * (page - 1),
    }

    groups = toolkit.get_action('group_list')(data_dict=data_dict)
    return groups


def package_showcase_list(context):
    '''Returns a list of showcase from a dataset
    '''
    showcase_list = []
    package_dict = toolkit.get_action('ckanext_package_showcase_list')(
        {}, {'package_id': context.pkg_dict['id']})
    for package in package_dict:
        showcase_dict = toolkit.get_action(
            'ckanext_showcase_show')({}, {'id': package['id']})
        showcase_list.append(showcase_dict)
    return showcase_list


def get_insuit_URL():
    '''Returns the Insuit accessibility URL script
    '''
    return config.get(
        'ckanext.gobcantheme.accessibility.insuit.url', None)


def get_readspeak_URL():
    '''Returns the Insuit accessibility URL script
    '''
    return config.get(
        'ckanext.gobcantheme.accessibility.readspeak.url', None)


# Map de vistas que se quieren restringir acceso
REDIRECT_VIEWS = (
    #route, view, function
    ('/user/register', 'abort_redirect', abort_redirect),
)


class GobcanthemePlugin(plugins.SingletonPlugin, DefaultTranslation):
    plugins.implements(plugins.IConfigurer)
    plugins.implements(plugins.IBlueprint)
    plugins.implements(plugins.ITranslation)
    plugins.implements(plugins.ITemplateHelpers)

    # IConfigurer

    def update_config(self, config_):
        toolkit.add_template_directory(config_, '../templates')
        toolkit.add_public_directory(config_, '../public')
        toolkit.add_resource('../assets', 'ckanext-gobcantheme')

    # IBlueprint

    def get_blueprint(self):
        gobcantheme = Blueprint('gobcantheme', self.__module__)

        gobcantheme.add_url_rule('/', 'dataset_redirect', dataset_redirect)

        if REDIRECT_VIEWS:
            for route_, view_, function_ in REDIRECT_VIEWS:
                gobcantheme.add_url_rule(route_, view_, function_)

        return [gobcantheme]

    # ITranslation

    def i18n_directory(self):
        return I18N_DIR

    # ITemplateHelpers

    def get_helpers(self):
        '''Register the get_all_groups function as a template helper function.
        '''
        return {
            'gobcan_theme_get_all_groups': get_all_groups,
            'package_showcase_list': package_showcase_list,
            'gobcan_theme_accessibility_insuit_url': get_insuit_URL,
            'gobcan_theme_accessibility_readspeak_url': get_readspeak_URL,
        }
