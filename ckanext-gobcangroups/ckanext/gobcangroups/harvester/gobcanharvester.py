import json
import uuid
import logging
import traceback

import ckan.plugins as p
import ckan.model as model
import ckan.lib.plugins as lib_plugins

from ckanext.harvest.model import HarvestObject

from ckanext.dcat.harvesters.rdf import DCATRDFHarvester

from ckanext.dcat.interfaces import IDCATRDFHarvester


log = logging.getLogger(__name__)


class GOBCANHarvester(DCATRDFHarvester):

    def info(self):
        return {
            'name': 'gobcan_harvester',
            'title': 'GOBCAN DCAT RDF Harvester',
            'description': 'DCAT RDF Harvester modified for GOBCAN'
        }

    def _search_dict(self, list_of_dictionaries, key, value):
        return [element for element in list_of_dictionaries if element[key] == value]

    def _check_value_in_group(self, group, theme):
        for data in group:
            if data['value'] in theme[0]['value']:
                return True

    def _add_dataset_to_group(self, dataset, context, harvest_object):
        try:
            if self._search_dict(dataset['extras'], 'key', 'theme'):
                groups = p.toolkit.get_action('group_list')(
                    data_dict={'all_fields': True, 'include_extras': True})
                theme = self._search_dict(dataset['extras'], 'key', 'theme')
                for group in groups:
                    group_extras = self._search_dict(
                        group['extras'], 'key', 'skos_nti')
                    if self._check_value_in_group(group_extras, theme):
                        data_dict = {"id": group['id'],
                                     "object": dataset['id'],
                                     "object_type": 'package',
                                     "capacity": 'public'}
                        p.toolkit.get_action('member_create')(
                            context, data_dict)
                        log.info(
                            'Created dataset group member for the group %s' % group['id'])
        except p.toolkit.ValidationError as e:
            self._save_object_error('Create validation Error: %s' % str(
                e.error_summary), harvest_object, 'Import')
            return False

    def _add_dataset_to_organization(self, dataset, context, harvest_object):
        if self._search_dict(dataset['extras'], 'key', 'publisher_uri'):
            organizations = p.toolkit.get_action(
                'organization_list')(
                    data_dict={'all_fields': True, 'include_extras': True})
            publisher = self._search_dict(
                dataset['extras'], 'key', 'publisher_uri')
            for organization in organizations:
                organizations_extras = self._search_dict(
                    organization['extras'], 'key', 'dir3')
                if self._check_value_in_group(organizations_extras, publisher):
                    return organization['id']

    def _set_organization(self, harvest_object, context, dataset):
        organization = self._add_dataset_to_organization(
            dataset, context, harvest_object)
        if organization is not None:
            dataset['owner_org'] = organization

    def import_stage(self, harvest_object):

        log.debug('In GOBCANHarvester import_stage')

        status = self._get_object_extra(harvest_object, 'status')
        if status == 'delete':
            # Delete package
            context = {'model': model, 'session': model.Session,
                       'user': self._get_user_name(), 'ignore_auth': True}

            p.toolkit.get_action('package_delete')(
                context, {'id': harvest_object.package_id})
            log.info('Deleted package {0} with guid {1}'.format(harvest_object.package_id,
                                                                harvest_object.guid))
            return True

        if harvest_object.content is None:
            self._save_object_error('Empty content for object {0}'.format(harvest_object.id),
                                    harvest_object, 'Import')
            return False

        try:
            dataset = json.loads(harvest_object.content)
        except ValueError:
            self._save_object_error('Could not parse content for object {0}'.format(harvest_object.id),
                                    harvest_object, 'Import')
            return False

        # Get the last harvested object (if any)
        previous_object = model.Session.query(HarvestObject) \
                                       .filter(HarvestObject.guid == harvest_object.guid) \
                                       .filter(HarvestObject.current == True) \
                                       .first()

        # Flag previous object as not current anymore
        if previous_object:
            previous_object.current = False
            previous_object.add()

        # Flag this object as the current one
        harvest_object.current = True
        harvest_object.add()

        context = {
            'user': self._get_user_name(),
            'return_id_only': True,
            'ignore_auth': True,
        }

        dataset = self.modify_package_dict(dataset, {}, harvest_object)

        # Check if a dataset with the same guid exists
        existing_dataset = self._get_existing_dataset(harvest_object.guid)

        try:
            if existing_dataset:
                # Don't change the dataset name even if the title has
                dataset['name'] = existing_dataset['name']
                dataset['id'] = existing_dataset['id']

                harvester_tmp_dict = {}

                # check if resources already exist based on their URI
                existing_resources = existing_dataset.get('resources')
                resource_mapping = {r.get('uri'): r.get('id')
                                    for r in existing_resources if r.get('uri')}
                for resource in dataset.get('resources'):
                    res_uri = resource.get('uri')
                    if res_uri and res_uri in resource_mapping:
                        resource['id'] = resource_mapping[res_uri]

                for harvester in p.PluginImplementations(IDCATRDFHarvester):
                    harvester.before_update(
                        harvest_object, dataset, harvester_tmp_dict)

                try:
                    if dataset:

                        self._set_organization(
                            harvest_object, context, dataset)

                        # Save reference to the package on the object
                        harvest_object.package_id = dataset['id']
                        harvest_object.add()

                        p.toolkit.get_action(
                            'package_update')(context, dataset)
                    else:
                        log.info('Ignoring dataset %s' %
                                 existing_dataset['name'])
                        return 'unchanged'
                except p.toolkit.ValidationError as e:
                    self._save_object_error('Update validation Error: %s' % str(
                        e.error_summary), harvest_object, 'Import')
                    return False

                for harvester in p.PluginImplementations(IDCATRDFHarvester):
                    err = harvester.after_update(
                        harvest_object, dataset, harvester_tmp_dict)

                    if err:
                        self._save_object_error(
                            'RDFHarvester plugin error: %s' % err, harvest_object, 'Import')
                        return False
                log.info('Updated dataset %s' % dataset['name'])

                self._add_dataset_to_group(dataset, context, harvest_object)

            else:
                package_plugin = lib_plugins.lookup_package_plugin(
                    dataset.get('type', None))

                package_schema = package_plugin.create_package_schema()
                context['schema'] = package_schema

                # We need to explicitly provide a package ID
                dataset['id'] = str(uuid.uuid4())
                package_schema['id'] = [str]

                harvester_tmp_dict = {}

                name = dataset['name']
                for harvester in p.PluginImplementations(IDCATRDFHarvester):
                    harvester.before_create(
                        harvest_object, dataset, harvester_tmp_dict)

                try:
                    if dataset:

                        self._set_organization(
                            harvest_object, context, dataset)

                        # Save reference to the package on the object
                        harvest_object.package_id = dataset['id']
                        harvest_object.add()

                        # Defer constraints and flush so the dataset can be indexed with
                        # the harvest object id (on the after_show hook from the harvester
                        # plugin)
                        model.Session.execute(
                            'SET CONSTRAINTS harvest_object_package_id_fkey DEFERRED')
                        model.Session.flush()

                        p.toolkit.get_action(
                            'package_create')(context, dataset)
                    else:
                        log.info('Ignoring dataset %s' % name)
                        return 'unchanged'
                except p.toolkit.ValidationError as e:
                    self._save_object_error('Create validation Error: %s' % str(
                        e.error_summary), harvest_object, 'Import')
                    return False

                for harvester in p.PluginImplementations(IDCATRDFHarvester):
                    err = harvester.after_create(
                        harvest_object, dataset, harvester_tmp_dict)

                    if err:
                        self._save_object_error(
                            'RDFHarvester plugin error: %s' % err, harvest_object, 'Import')
                        return False

                log.info('Created dataset %s' % dataset['name'])

                self._add_dataset_to_group(
                    dataset, context, harvest_object)

        except Exception as e:
            self._save_object_error('Error importing dataset %s: %r / %s' % (
                dataset.get('name', ''), e, traceback.format_exc()), harvest_object, 'Import')
            return False

        finally:
            model.Session.commit()

        return True
