import os
import uuid
import logging
import copy

import requests
import rdflib
import html2text

from ckan import plugins as p
from ckan import logic
from ckan import model
from ckan.lib.helpers import json
from ckan.lib import helpers as h
from ckan.logic import ValidationError, NotFound, get_action
from ckan.logic.converters import convert_group_name_or_id_to_id
from ckan.plugins import toolkit

from ckanext.harvest.harvesters import HarvesterBase
from ckanext.harvest.model import HarvestObject, HarvestObjectExtra


log = logging.getLogger(__name__)
CUSTOM_FIELDS = [
'private',
'searchable',
'publisher_uri',
'contact_email',
'contact_name',
'contact_uri',
'frequency',
'publisher_email',
'spatial',
'temporal',
'theme',
'version_notes',
'license_id'
]
from ckanext.dcat.interfaces import IDCATRDFHarvester


log = logging.getLogger(__name__)


class DCATHarvester(HarvesterBase):

    MAX_FILE_SIZE = 1024 * 1024 * 50  # 50 Mb
    CHUNK_SIZE = 1024

    force_import = False

    _user_name = None

    def _get_content_and_type(self, url, harvest_job, page=1,
                              content_type=None):
        '''
        Gets the content and type of the given url.

        :param url: a web url (starting with http) or a local path
        :param harvest_job: the job, used for error reporting
        :param page: adds paging to the url
        :param content_type: will be returned as type
        :return: a tuple containing the content and content-type
        '''

        if not url.lower().startswith('http'):
            # Check local file
            if os.path.exists(url):
                with open(url, 'r') as f:
                    content = f.read()
                content_type = content_type or rdflib.util.guess_format(url)
                return content, content_type
            else:
                self._save_gather_error('Could not get content for this url',
                                        harvest_job)
                return None, None

        try:
            if page > 1:
                url = url + '&' if '?' in url else url + '?'
                url = url + 'page={0}'.format(page)

            log.debug('Getting file %s', url)

            # get the `requests` session object
            session = requests.Session()
            for harvester in p.PluginImplementations(IDCATRDFHarvester):
                session = harvester.update_session(session)

            # first we try a HEAD request which may not be supported
            did_get = False
            r = session.head(url)
            if r.status_code == 405 or r.status_code == 400:
                r = session.get(url, stream=True)
                did_get = True
            r.raise_for_status()

            cl = r.headers.get('content-length')
            if cl and int(cl) > self.MAX_FILE_SIZE:
                msg = '''Remote file is too big. Allowed
                    file size: {allowed}, Content-Length: {actual}.'''.format(
                    allowed=self.MAX_FILE_SIZE, actual=cl)
                self._save_gather_error(msg, harvest_job)
                return None, None

            if not did_get:
                r = session.get(url, stream=True)

            length = 0
            content = ''
            for chunk in r.iter_content(chunk_size=self.CHUNK_SIZE):
                content = content + chunk
                length += len(chunk)

                if length >= self.MAX_FILE_SIZE:
                    self._save_gather_error('Remote file is too big.',
                                            harvest_job)
                    return None, None

            if content_type is None and r.headers.get('content-type'):
                content_type = r.headers.get('content-type').split(";", 1)[0]

            return content, content_type

        except requests.exceptions.HTTPError, error:
            if page > 1 and error.response.status_code == 404:
                # We want to catch these ones later on
                raise

            msg = 'Could not get content from %s. Server responded with %s %s'\
                % (url, error.response.status_code, error.response.reason)
            self._save_gather_error(msg, harvest_job)
            return None, None
        except requests.exceptions.ConnectionError, error:
            msg = '''Could not get content from %s because a
                                connection error occurred. %s''' % (url, error)
            self._save_gather_error(msg, harvest_job)
            return None, None
        except requests.exceptions.Timeout, error:
            msg = 'Could not get content from %s because the connection timed'\
                ' out.' % url
            self._save_gather_error(msg, harvest_job)
            return None, None

    def _get_user_name(self):
        if self._user_name:
            return self._user_name

        user = p.toolkit.get_action('get_site_user')(
            {'ignore_auth': True, 'defer_commit': True},
            {})
        self._user_name = user['name']

        return self._user_name

    def _get_object_extra(self, harvest_object, key):
        '''
        Helper function for retrieving the value from a harvest object extra,
        given the key
        '''
        for extra in harvest_object.extras:
            if extra.key == key:
                return extra.value
        return None

    def _get_package_name(self, harvest_object, title):

        package = harvest_object.package
        if package is None or package.title != title:
            name = self._gen_new_name(title)
            if not name:
                raise Exception(
                    'Could not generate a unique name from the title or the '
                    'GUID. Please choose a more unique title.')
        else:
            name = package.name

        return name

    def get_original_url(self, harvest_object_id):
        obj = model.Session.query(HarvestObject). \
            filter(HarvestObject.id == harvest_object_id).\
            first()
        if obj:
            return obj.source.url
        return None



    ## Start hooks

    def modify_package_dict(self, package_dict, dcat_dict, harvest_object):
        '''
            Allows custom harvesters to modify the package dict before
            creating or updating the actual package.
        '''
        return package_dict

    # End hooks

    def _set_config(self, config_str):
        if config_str:
            self.config = json.loads(config_str)
            if 'api_version' in self.config:
                self.api_version = int(self.config['api_version'])
        else:
            self.config = {}

    def validate_config(self, config):
        if not config:
            return config

        try:
            config_obj = json.loads(config)

            if 'api_version' in config_obj:
                try:
                    int(config_obj['api_version'])
                except ValueError:
                    raise ValueError('api_version must be an integer')

            if 'default_tags' in config_obj:
                if not isinstance(config_obj['default_tags'], list):
                    raise ValueError('default_tags must be a list')

            if 'default_groups' in config_obj:
                if not isinstance(config_obj['default_groups'], list):
                    raise ValueError('default_groups must be a list')

                # Check if default groups exist
                context = {'model': model, 'user': toolkit.c.user}
                for group_name in config_obj['default_groups']:
                    try:
                        group = get_action('group_show')(
                            context, {'id': group_name})
                    except NotFound, e:
                        raise ValueError('Default group not found')

            if 'default_extras' in config_obj:
                if not isinstance(config_obj['default_extras'], dict):
                    raise ValueError('default_extras must be a dictionary')

        except ValueError, e:
            raise e

        return config


    def gather_stage(self,harvest_job):
        log.debug('In DCATHarvester gather_stage')


        ids = []

        # gil
        print harvest_job
        self._set_config(harvest_job.source.config)
        log.debug('Using config: %r', self.config)
        # gil

        # Get the previous guids for this source
        query = model.Session.query(HarvestObject.guid, HarvestObject.package_id).\
                                    filter(HarvestObject.current==True).\
                                    filter(HarvestObject.harvest_source_id==harvest_job.source.id)
        guid_to_package_id = {}

        for guid, package_id in query:
            guid_to_package_id[guid] = package_id

        guids_in_db = guid_to_package_id.keys()
        guids_in_source = []


        # Get file contents
        url = harvest_job.source.url

        previous_guids = []
        page = 1
        while True:

            try:
                content, content_type = self._get_content_and_type(url, harvest_job, page)
            except requests.exceptions.HTTPError, error:
                if error.response.status_code == 404:
                    if page > 1:
                        # Server returned a 404 after the first page, no more
                        # records
                        log.debug('404 after first page, no more pages')
                        break
                    else:
                        # Proper 404
                        msg = 'Could not get content. Server responded with 404 Not Found'
                        self._save_gather_error(msg, harvest_job)
                        return None
                else:
                    # This should never happen. Raising just in case.
                    raise

            if not content:
                return None


            try:

                batch_guids = []
                for guid, as_string in self._get_guids_and_datasets(content):

                    log.debug('Got identifier: {0}'.format(guid.encode('utf8')))
                    batch_guids.append(guid)

                    if guid not in previous_guids:

                        if guid in guids_in_db:
                            # Dataset needs to be udpated
                            obj = HarvestObject(guid=guid, job=harvest_job,
                                            package_id=guid_to_package_id[guid],
                                            content=as_string,
                                            extras=[HarvestObjectExtra(key='status', value='change')])
                        else:
                            # Dataset needs to be created
                            obj = HarvestObject(guid=guid, job=harvest_job,
                                            content=as_string,
                                            extras=[HarvestObjectExtra(key='status', value='new')])
                        obj.save()
                        ids.append(obj.id)

                if len(batch_guids) > 0:
                    guids_in_source.extend(set(batch_guids) - set(previous_guids))
                else:
                    log.debug('Empty document, no more records')
                    # Empty document, no more ids
                    break

            except ValueError, e:
                msg = 'Error parsing file: {0}'.format(str(e))
                self._save_gather_error(msg, harvest_job)
                return None

            if sorted(previous_guids) == sorted(batch_guids):
                # Server does not support pagination or no more pages
                log.debug('Same content, no more pages')
                break


            page = page + 1

            previous_guids = batch_guids

        # Check datasets that need to be deleted
        guids_to_delete = set(guids_in_db) - set(guids_in_source)
        for guid in guids_to_delete:
            obj = HarvestObject(guid=guid, job=harvest_job,
                                package_id=guid_to_package_id[guid],
                                extras=[HarvestObjectExtra(key='status', value='delete')])
            ids.append(obj.id)
            model.Session.query(HarvestObject).\
                  filter_by(guid=guid).\
                  update({'current': False}, False)
            obj.save()


        return ids

    def fetch_stage(self,harvest_object):
        return True

    def import_stage(self,harvest_object):
        log.debug('In DCATHarvester import_stage')
        if not harvest_object:
            log.error('No harvest object received')
            return False

        if self.force_import:
            status = 'change'
        else:
            status = self._get_object_extra(harvest_object, 'status')

        if status == 'delete':
            # Delete package
            context = {'model': model, 'session': model.Session, 'user': self._get_user_name()}

            p.toolkit.get_action('package_delete')(context, {'id': harvest_object.package_id})
            log.info('Deleted package {0} with guid {1}'.format(harvest_object.package_id, harvest_object.guid))

            return True


        if harvest_object.content is None:
            self._save_object_error('Empty content for object %s' % harvest_object.id,harvest_object,'Import')
            return False

        # Get the last harvested object (if any)
        previous_object = model.Session.query(HarvestObject) \
                          .filter(HarvestObject.guid==harvest_object.guid) \
                          .filter(HarvestObject.current==True) \
                          .first()

        # Flag previous object as not current anymore
        if previous_object and not self.force_import:
            previous_object.current = False
            previous_object.add()


        package_dict, dcat_dict = self._get_package_dict(harvest_object)
        if not package_dict:
            return False

        if not package_dict.get('name'):
            package_dict['name'] = self._get_package_name(harvest_object, package_dict['title'])


        # gil

        self._set_config(harvest_object.job.source.config)

        try:
            # We need to explicitly provide a package ID
            package_dict['id'] = unicode(uuid.uuid4())

            if package_dict.get('type') == 'harvest':
                log.warn('Remote dataset is a harvest source, ignoring...')
                return True

            # Set default tags if needed
            default_tags = self.config.get('default_tags', [])
            if default_tags:
                if not 'tags' in package_dict:
                    package_dict['tags'] = []

                package_dict['tags'].extend(
                    [t for t in default_tags if t not in package_dict['tags']])

            # Set default extras if needed
            default_extras = self.config.get('default_extras', {})

            def get_extra(key, package_dict):
                for extra in package_dict.get('extras', []):
                    if extra['key'] == key:
                        return extra

            if default_extras:
                override_extras = self.config.get('override_extras', False)

                if not 'extras' in package_dict:
                    package_dict['extras'] = {}
                for key, value in default_extras.iteritems():
                    existing_extra = get_extra(key, package_dict)
                    if existing_extra and not override_extras:
                        continue  # no need for the default
                    if existing_extra:
                        package_dict['extras'].remove(existing_extra)
                    # Look for replacement strings
                    if isinstance(value, basestring):
                        value = value.format(
                            harvest_source_id=harvest_object.job.source.id,
                            harvest_source_url=harvest_object.job.source.url.strip('/'),
                            harvest_source_title=harvest_object.job.source.title,
                            harvest_job_id=harvest_object.job.id,
                            harvest_object_id=harvest_object.id,
                            dataset_id=package_dict['id']
                        )
                    package_dict['extras'].append({'key': key, 'value': value})

            # Set Group/Thema (if possible)
            extra_theme = next((item for item in package_dict['extras'] if item["key"] == "theme"), False)
            # Check if thema field is set
            if extra_theme:
                for g in h.groups_available():
                    if (extra_theme['value'] == g['name'] or extra_theme['value'].lower() == g['display_name'].lower()):
                        if not 'groups' in package_dict:
                            package_dict['groups'] = []
                        package_dict['groups'] = [{"name": extra_theme['value']}]
            else:
                # Check if any of the Tags corresponds to a existing group
                if 'tags' in package_dict:
                    for t in package_dict['tags']:
                        for g in h.groups_available():
                            if (t['name'] == g['name'] or t['name'].lower() == g['display_name'].lower()):
                                if not 'groups' in package_dict:
                                    package_dict['groups'] = []
                                package_dict['groups'] = [{"name": g['name']}]
                if 'groups' in package_dict:
                    package_dict['extras'].append({'key': 'theme', 'value': package_dict['groups'][0]['name']})

                    # Descrption: Renders HTML to plain text (Markdown compatible)
            if 'notes' in package_dict:
                package_dict['notes'] = html2text.html2text(package_dict['notes'])

            for resource in package_dict.get('resources', []):
                # Clear remote url_type for resources (eg datastore, upload) as
                # we are only creating normal resources with links to the
                # remote ones
                resource.pop('url_type', None)
                # Clear revision_id as the revision won't exist on this CKAN
                # and saving it will cause an IntegrityError with the foreign
                # key.
                resource.pop('revision_id', None)
                # result = self._create_or_update_package(
                #     package_dict, harvest_object, package_dict_form='package_show')

                # return result
        except ValidationError, e:
            self._save_object_error('Invalid package with GUID %s: %r' %
                                    (harvest_object.guid, e.error_dict),
                                    harvest_object, 'Import')
        except Exception, e:
            self._save_object_error('%s' % e, harvest_object, 'Import')

        # gil


        # Allow custom harvesters to modify the package dict before creating
        # or updating the package
        package_dict = self.modify_package_dict(package_dict,
                                                dcat_dict,
                                                harvest_object)
        # Unless already set by an extension, get the owner organization (if any)
        # from the harvest source dataset
        if not package_dict.get('owner_org'):
            source_dataset = model.Package.get(harvest_object.source.id)
            if source_dataset.owner_org:
                package_dict['owner_org'] = source_dataset.owner_org

        # Flag this object as the current one
        harvest_object.current = True
        harvest_object.add()

        context = {
            'user': self._get_user_name(),
            'return_id_only': True,
            'ignore_auth': True,
        }

        if status == 'new':


            package_schema = logic.schema.default_create_package_schema()
            context['schema'] = package_schema

            # We need to explicitly provide a package ID
            package_dict['id'] = unicode(uuid.uuid4())
            package_schema['id'] = [unicode]

            # Save reference to the package on the object
            harvest_object.package_id = package_dict['id']
            harvest_object.add()

            # Defer constraints and flush so the dataset can be indexed with
            # the harvest object id (on the after_show hook from the harvester
            # plugin)
            model.Session.execute('SET CONSTRAINTS harvest_object_package_id_fkey DEFERRED')
            model.Session.flush()

            # gil
            # Check Extras Against the default Schema
            override_extras = self.config.get('override_extras', False)

            iterable_extras = list(package_dict['extras'])
            for extra in iterable_extras:
                if extra['key'] in package_schema:
                    # take it out of extras and add it to package_dict as a normal field
                    package_dict[extra['key']] = extra['value']
                    package_dict['extras'].remove(extra)

            log.debug('package_dict = {0}'.format(package_dict))
            # gil

            package_id = p.toolkit.get_action('package_create')(context, package_dict)
            log.info('Created dataset with id %s', package_id)
        elif status == 'change':

            package_dict['id'] = harvest_object.package_id
            # gil

            # Check Extras Against Schema from the object retrieved
            existing_package = p.toolkit.get_action('package_show')(context, {'id': harvest_object.package_id})
            override_extras = self.config.get('override_extras', False)

            iterable_extras = list(package_dict['extras'])
            for extra in iterable_extras:
                if extra['key'] in existing_package:
                    if override_extras:
                        package_dict[extra['key']] = extra['value']
                        package_dict['extras'].remove(extra)
                        continue
                    else:
                        package_dict[extra['key']] = existing_package[extra['key']]
                        package_dict['extras'].remove(extra)
                        continue
                elif extra['key'] in existing_package['extras']:
                    if override_extras:
                        log.debug('Guia 6')
                        package_dict['extras'][extra['key']] = extra['value']
                        continue
                elif extra['key'] in CUSTOM_FIELDS:
                    # take it out of extras and add it to package_dict as a normal field
                    package_dict[extra['key']] = extra['value']
                    package_dict['extras'].remove(extra)

            # log.debug('package_dict = {0}'.format(package_dict))

            # gil
            package_id = p.toolkit.get_action('package_update')(context, package_dict)
            log.info('Updated dataset with id %s', package_id)

        model.Session.commit()

        return True

