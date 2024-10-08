import botocore
import httmock
import inspect
import io
import json
import moto
import os
import shutil
import tempfile
import time
import unittest.mock
import zipfile

from .. import base, mock_s3
from girder import events
from girder.constants import AssetstoreType, ROOT_DIR
from girder.models.assetstore import Assetstore
from girder.models.file import File
from girder.models.folder import Folder
from girder.models.item import Item
from girder.exceptions import GirderException
from girder.models.upload import Upload
from girder.models.user import User
from girder.utility import assetstore_utilities
from girder.utility.progress import ProgressContext
from girder.utility.s3_assetstore_adapter import makeBotoConnectParams
from girder.utility import path as path_util


def setUpModule():
    base.startServer()


def tearDownModule():
    base.stopServer()


class AssetstoreTestCase(base.TestCase):

    def setUp(self):
        super().setUp()

        info = {
            'email': 'admin@girder.test',
            'login': 'admin',
            'firstName': 'Admin',
            'lastName': 'Admin',
            'password': 'adminpassword',
            'admin': True
        }
        self.admin = User().createUser(**info)

    def testCreateAndSetCurrent(self):
        # Non admin users should not be able to see assetstore list
        resp = self.request(path='/assetstore', method='GET')
        self.assertStatus(resp, 401)

        resp = self.request(path='/assetstore', method='GET', user=self.admin)
        self.assertStatusOk(resp)
        self.assertEqual(1, len(resp.json))
        oldAssetstore = resp.json[0]

        self.assertTrue(oldAssetstore['current'])
        self.assertEqual(oldAssetstore['name'], 'Test')
        self.assertEqual(oldAssetstore['type'], AssetstoreType.FILESYSTEM)
        self.assertEqual(oldAssetstore['perms'], 0o600)

        params = {
            'name': 'Test',
            'type': -1
        }
        resp = self.request(path='/assetstore', method='POST', user=self.admin, params=params)
        self.assertStatus(resp, 400)
        self.assertEqual(resp.json, {
            'type': 'rest',
            'message': 'Invalid type parameter'
        })

        params = {
            'name': 'Test',
            'type': AssetstoreType.FILESYSTEM
        }
        resp = self.request(path='/assetstore', method='POST', user=self.admin, params=params)
        self.assertMissingParameter(resp, 'root')

        params['root'] = os.path.join(oldAssetstore['root'], 'other')
        resp = self.request(path='/assetstore', method='POST', user=self.admin, params=params)
        self.assertStatus(resp, 400)
        self.assertEqual(resp.json['field'], 'name')

        params['name'] = 'New Name'
        # Actually creates the new assetstore
        resp = self.request(path='/assetstore', method='POST', user=self.admin, params=params)
        self.assertStatusOk(resp)
        assetstore = resp.json
        self.assertEqual(assetstore['name'], 'New Name')
        self.assertFalse(assetstore['current'])

        # Test validation of file permissions
        params = {
            'name': assetstore['name'],
            'root': assetstore['root'],
            'current': True,
            'perms': '384'
        }
        resp = self.request(path='/assetstore/%s' % assetstore['_id'],
                            method='PUT', user=self.admin, params=params)
        self.assertStatus(resp, 400)
        self.assertEqual(
            resp.json['message'], 'File permissions must be an octal integer.')

        params['perms'] = '400'
        resp = self.request(path='/assetstore/%s' % assetstore['_id'],
                            method='PUT', user=self.admin, params=params)
        self.assertStatus(resp, 400)
        self.assertEqual(
            resp.json['message'], 'File permissions must allow "rw" for user.')

        # Set the new assetstore as current
        params['perms'] = '755'
        resp = self.request(path='/assetstore/%s' % assetstore['_id'],
                            method='PUT', user=self.admin, params=params)
        self.assertStatusOk(resp)
        assetstore = Assetstore().load(resp.json['_id'])
        self.assertTrue(assetstore['current'])

        # The old assetstore should no longer be current
        oldAssetstore = Assetstore().load(oldAssetstore['_id'])
        self.assertFalse(oldAssetstore['current'])

        # List the assetstores
        assetstoresBefore = list(Assetstore().list())
        # Now break the root of the new assetstore and make sure we can still
        # list it
        oldroot = assetstore['root']
        assetstore['root'] = os.path.join(os.devnull, 'invalidpath')
        assetstore = Assetstore().save(assetstore, validate=False)
        assetstoresAfter = list(Assetstore().list())
        self.assertEqual(len(assetstoresBefore), len(assetstoresAfter))
        self.assertIsNone([
            store for store in assetstoresAfter
            if store['_id'] == assetstore['_id']][0]['capacity']['free'])
        # restore the original root
        assetstore['root'] = oldroot
        Assetstore().save(assetstore, validate=False)

    def testFilesystemAssetstoreImport(self):
        folder = next(Folder().childFolders(
            self.admin, parentType='user', force=True, filters={
                'name': 'Public'
            }))

        params = {
            'importPath': '/nonexistent/dir',
            'destinationType': 'folder',
            'destinationId': folder['_id']
        }
        path = '/assetstore/%s/import' % str(self.assetstore['_id'])

        resp = self.request(path, method='POST', params=params)
        self.assertStatus(resp, 401)

        resp = self.request(path, method='POST', params=params, user=self.admin)
        self.assertStatus(resp, 400)
        self.assertEqual(resp.json['message'], 'Not found: /nonexistent/dir.')

        # Test importing a single file
        params['importPath'] = os.path.join(
            ROOT_DIR, 'tests', 'cases', 'py_client', 'testdata', 'world.txt')
        resp = self.request(path, method='POST', params=params, user=self.admin)
        self.assertStatusOk(resp)
        resp = self.request('/resource/lookup', user=self.admin, params={
            'path': '/user/admin/Public/world.txt/world.txt'
        })
        self.assertStatusOk(resp)
        self.assertEqual(resp.json['_modelType'], 'file')
        file = File().load(resp.json['_id'], force=True, exc=True)
        self.assertTrue(os.path.isfile(file['path']))

        File().remove(file)

        # Test importing directory with include & exclude regexes; file should be excluded
        params['importPath'] = os.path.join(ROOT_DIR, 'tests', 'cases', 'py_client')
        resp = self.request(
            path, method='POST', user=self.admin, params=dict(
                params, fileIncludeRegex='world.*', fileExcludeRegex='world.*'))
        self.assertStatusOk(resp)

        resp = self.request('/resource/lookup', user=self.admin, params={
            'path': '/user/admin/Public/world.txt/world.txt'
        })
        self.assertStatus(resp, 400)

        # Do the import with the include regex on
        resp = self.request(
            path, method='POST', params=dict(params, fileIncludeRegex='hello.*'), user=self.admin)
        self.assertStatusOk(resp)

        # hello.txt should have been imported
        resp = self.request('/resource/lookup', user=self.admin, params={
            'path': '/user/admin/Public/testdata/hello.txt/hello.txt'
        })
        self.assertStatusOk(resp)
        self.assertEqual(resp.json['_modelType'], 'file')
        file = File().load(resp.json['_id'], force=True, exc=True)

        self.assertTrue(os.path.isfile(file['path']))

        # world.txt should not
        resp = self.request('/resource/lookup', user=self.admin, params={
            'path': '/user/admin/Public/world.txt/world.txt'
        })
        self.assertStatus(resp, 400)

        # Run import without any regexes specified, all files should be imported
        resp = self.request(path, method='POST', params=params, user=self.admin)
        self.assertStatusOk(resp)

        resp = self.request('/resource/lookup', user=self.admin, params={
            'path': '/user/admin/Public/testdata/world.txt/world.txt'
        })
        self.assertStatusOk(resp)
        self.assertEqual(resp.json['_modelType'], 'file')

        # Make sure downloading the file works
        resp = self.request('/file/%s/download' % str(file['_id']), isJson=False)
        self.assertStatusOk(resp)
        self.assertEqual(self.getBody(resp), 'hello\n')

        # Deleting the file should not actually remove the file on disk
        resp = self.request('/file/' + str(file['_id']), method='DELETE', user=self.admin)
        self.assertStatusOk(resp)

        self.assertIsNone(File().load(file['_id'], force=True))
        self.assertTrue(os.path.isfile(file['path']))

        # Attempt to import a folder with an item directly into user; should fail
        resp = self.request(
            '/assetstore/%s/import' % self.assetstore['_id'], method='POST', params={
                'importPath': os.path.join(
                    ROOT_DIR, 'tests', 'cases', 'py_client', 'testdata'),
                'destinationType': 'user',
                'destinationId': self.admin['_id']
            }, user=self.admin)
        self.assertStatus(resp, 400)
        self.assertEqual(
            resp.json['message'], 'Files cannot be imported directly underneath a user.')

    def testFilesystemAssetstoreImportLeafFoldersAsItems(self):
        folder = next(Folder().childFolders(
            self.admin, parentType='user', force=True, filters={
                'name': 'Public'
            }))

        params = {
            'importPath': os.path.join(ROOT_DIR, 'tests', 'cases', 'py_client', 'testdata'),
            'destinationType': 'folder',
            'destinationId': folder['_id'],
            'leafFoldersAsItems': 'true'
        }
        path = '/assetstore/%s/import' % str(self.assetstore['_id'])
        resp = self.request(path, method='POST', params=params, user=self.admin)
        self.assertStatusOk(resp)

        resp = self.request('/resource/lookup', user=self.admin, params={
            'path': '/user/admin/Public/testdata'
        })
        self.assertStatusOk(resp)
        self.assertEqual(resp.json['_modelType'], 'item')

        resp = self.request('/resource/lookup', user=self.admin, params={
            'path': '/user/admin/Public/testdata/hello.txt'
        })
        _file = File().load(resp.json['_id'], force=True, exc=True)

        self.assertTrue(os.path.isfile(_file['path']))

        # Make sure downloading the file works
        resp = self.request('/file/%s/download' % str(_file['_id']), isJson=False)
        self.assertStatusOk(resp)
        self.assertEqual(self.getBody(resp), 'hello\n')

        # Deleting the file should not actually remove the file on disk
        resp = self.request('/file/' + str(_file['_id']), method='DELETE', user=self.admin)
        self.assertStatusOk(resp)

        self.assertIsNone(File().load(_file['_id'], force=True))
        self.assertTrue(os.path.isfile(_file['path']))

    def testFilesystemAssetstoreWhitespaceInNames(self):
        srcdir = os.path.join(ROOT_DIR, 'tests', 'cases', 'py_client', 'testdata')
        with tempfile.TemporaryDirectory() as tmpdir:
            spacedir = os.path.join(tmpdir, ' Space \n ')
            os.mkdir(spacedir)
            spacefile = os.path.join(spacedir, ' \n hello.txt ')
            shutil.copy(os.path.join(srcdir, 'hello.txt'), spacefile)
            folder = next(Folder().childFolders(
                self.admin, parentType='user', force=True, filters={
                    'name': 'Public'
                }))

            params = {
                'importPath': tmpdir,
                'destinationType': 'folder',
                'destinationId': folder['_id'],
            }
            path = '/assetstore/%s/import' % str(self.assetstore['_id'])
            resp = self.request(path, method='POST', params=params, user=self.admin)
            self.assertStatusOk(resp)

            resp = self.request('/resource/lookup', user=self.admin, params={
                'path': '/user/admin/Public/_Space___/___hello.txt_'
            })
            self.assertStatusOk(resp)
            self.assertEqual(resp.json['_modelType'], 'item')

    def testFilesystemAssetstoreFindInvalidFiles(self):
        # Create several files in the assetstore, some of which point to real
        # files on disk and some that don't
        folder = next(Folder().childFolders(
            parent=self.admin, parentType='user', force=True, limit=1))
        item = Item().createItem('test', self.admin, folder)

        path = os.path.join(
            ROOT_DIR, 'tests', 'cases', 'py_client', 'testdata', 'hello.txt')
        real = File().createFile(
            name='hello.txt', creator=self.admin, item=item,
            assetstore=self.assetstore, size=os.path.getsize(path))
        real['imported'] = True
        real['path'] = path
        File().save(real)

        fake = File().createFile(
            name='fake', creator=self.admin, item=item, size=1, assetstore=self.assetstore)
        fake['path'] = 'nonexistent/path/to/file'
        fake['sha512'] = '...'
        fake = File().save(fake)

        fakeImport = File().createFile(
            name='fakeImport', creator=self.admin, item=item, size=1, assetstore=self.assetstore)
        fakeImport['imported'] = True
        fakeImport['path'] = '/nonexistent/path/to/file'
        fakeImport = File().save(fakeImport)

        adapter = assetstore_utilities.getAssetstoreAdapter(self.assetstore)
        self.assertTrue(inspect.isgeneratorfunction(adapter.findInvalidFiles))

        with ProgressContext(True, user=self.admin, title='test') as p:
            invalidFiles = list(adapter.findInvalidFiles(progress=p, filters={
                'imported': True
            }))
            self.assertEqual(len(invalidFiles), 1)
            self.assertEqual(invalidFiles[0]['reason'], 'missing')
            self.assertEqual(invalidFiles[0]['file']['_id'], fakeImport['_id'])
            self.assertEqual(p.progress['data']['current'], 2)
            self.assertEqual(p.progress['data']['total'], 2)

            invalidFiles = list(adapter.findInvalidFiles(progress=p))
            self.assertEqual(len(invalidFiles), 2)
            for invalidFile in invalidFiles:
                self.assertEqual(invalidFile['reason'], 'missing')
                self.assertIn(invalidFile['file']['_id'], (fakeImport['_id'], fake['_id']))
            self.assertEqual(p.progress['data']['current'], 3)
            self.assertEqual(p.progress['data']['total'], 3)

    def testDeleteAssetstore(self):
        resp = self.request(path='/assetstore', method='GET', user=self.admin)
        self.assertStatusOk(resp)
        self.assertEqual(1, len(resp.json))
        assetstore = Assetstore().load(resp.json[0]['_id'])

        # Create a second assetstore so that when we delete the first one, the
        # current assetstore will be switched to the second one.
        secondStore = Assetstore().createFilesystemAssetstore(
            'Another Store',
            os.path.join(ROOT_DIR, 'tests', 'assetstore', 'server_assetstore_test2'))
        # make sure our original asset store is the current one
        current = Assetstore().getCurrent()
        self.assertEqual(current['_id'], assetstore['_id'])

        # Anonymous user should not be able to delete assetstores
        resp = self.request(path='/assetstore/%s' % assetstore['_id'], method='DELETE')
        self.assertStatus(resp, 401)

        # Simulate the existence of a file within the assetstore
        folders = Folder().childFolders(self.admin, 'user', user=self.admin)
        item = Item().createItem(name='x.txt', creator=self.admin, folder=next(folders))
        file = File().createFile(
            creator=self.admin, item=item, name='x.txt',
            size=1, assetstore=assetstore, mimeType='text/plain')
        file['sha512'] = 'x'  # add this dummy value to simulate real file

        resp = self.request(path='/assetstore/%s' % assetstore['_id'],
                            method='DELETE', user=self.admin)
        self.assertStatus(resp, 400)
        self.assertEqual(resp.json['message'], 'You may not delete an '
                         'assetstore that contains files.')
        # Delete the offending file, we can now delete the assetstore
        File().remove(file)
        resp = self.request(path='/assetstore/%s' % assetstore['_id'],
                            method='DELETE', user=self.admin)
        self.assertStatusOk(resp)
        self.assertEqual(resp.json['message'],
                         'Deleted assetstore %s.' % assetstore['name'])

        resp = self.request(path='/assetstore', method='GET', user=self.admin)
        self.assertStatusOk(resp)
        self.assertEqual(1, len(resp.json))

        # Get the current assetstore.  It should now be the second store we
        # created
        current = Assetstore().getCurrent()
        self.assertEqual(current['_id'], secondStore['_id'])

    def testGetAssetstoreFiles(self):
        resp = self.request(path='/assetstore', method='GET', user=self.admin)
        self.assertStatusOk(resp)
        self.assertEqual(1, len(resp.json))
        assetstore = Assetstore().load(resp.json[0]['_id'])

        # Simulate the existence of a file within the assetstore
        folders = Folder().childFolders(
            self.admin, 'user', user=self.admin)
        item = Item().createItem(name='x.txt', creator=self.admin, folder=next(folders))
        file = File().createFile(
            creator=self.admin, item=item, name='x.txt',
            size=1, assetstore=assetstore, mimeType='text/plain')
        file['sha512'] = 'x'  # add this dummy value to simulate real file

        # Make sure we see the file
        resp = self.request(path='/assetstore/%s/files' % assetstore['_id'],
                            method='GET', user=self.admin)
        self.assertStatus(resp, 200)
        self.assertEqual(len(resp.json), 1)
        self.assertEqual(resp.json[0]['name'], 'x.txt')
        # Remove the file and make sure we no longer see it.
        File().remove(file)
        resp = self.request(path='/assetstore/%s/files' % assetstore['_id'],
                            method='GET', user=self.admin)
        self.assertStatus(resp, 200)
        self.assertEqual(len(resp.json), 0)

    def testGridFSAssetstoreAdapter(self):
        resp = self.request(path='/assetstore', method='GET', user=self.admin)
        self.assertStatusOk(resp)
        self.assertEqual(1, len(resp.json))
        oldAssetstore = resp.json[0]

        self.assertTrue(oldAssetstore['current'])
        self.assertEqual(oldAssetstore['name'], 'Test')
        # Clear any old DB data
        base.dropGridFSDatabase('girder_test_assetstore_create_assetstore')
        params = {
            'name': 'New Name',
            'type': AssetstoreType.GRIDFS
        }
        resp = self.request(path='/assetstore', method='POST', user=self.admin, params=params)
        self.assertMissingParameter(resp, 'db')

        params['db'] = 'girder_test_assetstore_create_assetstore'
        resp = self.request(path='/assetstore', method='POST', user=self.admin, params=params)
        self.assertStatusOk(resp)
        assetstore = resp.json
        self.assertEqual(assetstore['name'], 'New Name')
        self.assertFalse(assetstore['current'])

        # Set the new assetstore as current
        params = {
            'name': assetstore['name'],
            'db': assetstore['db'],
            'current': True
        }
        resp = self.request(
            path='/assetstore/%s' % assetstore['_id'],
            method='PUT', user=self.admin, params=params)
        self.assertStatusOk(resp)
        assetstore = Assetstore().load(resp.json['_id'])
        self.assertTrue(assetstore['current'])

        # The old assetstore should no longer be current
        oldAssetstore = Assetstore().load(oldAssetstore['_id'])
        self.assertFalse(oldAssetstore['current'])

        # Test that we can create an assetstore with an alternate mongo host
        # and a replica set (but don't bother using an actual replica set).
        # Since we are faking the replicaset, we have to bypass validation so
        # we don't get exceptions from trying to connect to nonexistent hosts.
        # We also hack to make it the current assetstore without using validate.
        Assetstore().update({'current': True}, {'$set': {'current': False}})
        params = {
            'name': 'Replica Set Name',
            'type': AssetstoreType.GRIDFS,
            'db': 'girder_test_assetstore_create_rs_assetstore',
            'mongohost': 'mongodb://127.0.0.1:27080,127.0.0.1:27081,127.0.0.1:27082',
            'replicaset': 'replicaset',
            'current': True
        }
        Assetstore().save(params, validate=False)

        # Neither of the old assetstores should  be current
        oldAssetstore = Assetstore().load(oldAssetstore['_id'])
        self.assertFalse(oldAssetstore['current'])
        assetstore = Assetstore().load(assetstore['_id'])
        self.assertFalse(assetstore['current'])

        # Getting the assetstores should succeed, even though we can't connect
        # to the replica set.
        resp = self.request(path='/assetstore', method='GET', user=self.admin)
        self.assertStatusOk(resp)

    @moto.mock_s3
    def testS3AssetstoreAdapter(self):
        # Delete the default assetstore
        Assetstore().remove(self.assetstore)
        s3Regex = (r'^(https://s3.amazonaws.com(:443)?/bucketname/foo/bar|'
                   'https://bucketname.s3.amazonaws.com(:443)?/foo/bar)')

        params = {
            'name': 'S3 Assetstore',
            'type': AssetstoreType.S3,
            'bucket': '',
            'accessKeyId': 'someKey',
            'secret': 'someSecret',
            'prefix': '/foo/bar/'
        }

        # Validation should fail with empty bucket name
        resp = self.request(path='/assetstore', method='POST', user=self.admin, params=params)
        self.assertStatus(resp, 400)
        self.assertEqual(resp.json, {
            'type': 'validation',
            'field': 'bucket',
            'message': 'Bucket must not be empty.'
        })

        params['bucket'] = 'bucketname'
        # Validation should fail with a missing bucket
        resp = self.request(path='/assetstore', method='POST', user=self.admin, params=params)
        self.assertStatus(resp, 400)
        self.assertEqual(resp.json, {
            'type': 'validation',
            'field': 'bucket',
            'message': 'Unable to write into bucket "bucketname".'
        })

        # Validation should fail with a bogus service name
        params['service'] = 'ftp://nowhere'
        resp = self.request(path='/assetstore', method='POST', user=self.admin, params=params)
        self.assertStatus(resp, 400)
        del params['service']

        # Create a bucket (mocked using moto), so that we can create an assetstore in it
        botoParams = makeBotoConnectParams(params['accessKeyId'], params['secret'])
        client = mock_s3.createBucket(botoParams, 'bucketname')

        # Create an assetstore
        resp = self.request(path='/assetstore', method='POST', user=self.admin, params=params)
        self.assertStatusOk(resp)
        assetstore = Assetstore().load(resp.json['_id'])

        # Set the assetstore to current.  This is really to test the edit assetstore code.
        params['current'] = True
        resp = self.request(
            path='/assetstore/%s' % assetstore['_id'], method='PUT', user=self.admin, params=params)
        self.assertStatusOk(resp)

        # Test init for a single-chunk upload
        folders = Folder().childFolders(self.admin, 'user')
        parentFolder = next(folders)
        params = {
            'parentType': 'folder',
            'parentId': parentFolder['_id'],
            'name': 'My File.txt',
            'size': 1024,
            'mimeType': 'text/plain'
        }
        resp = self.request(path='/file', method='POST', user=self.admin, params=params)
        self.assertStatusOk(resp)
        self.assertEqual(resp.json['received'], 0)
        self.assertEqual(resp.json['size'], 1024)
        self.assertEqual(resp.json['behavior'], 's3')

        singleChunkUpload = resp.json
        s3Info = singleChunkUpload['s3']
        self.assertEqual(s3Info['chunked'], False)
        self.assertIsInstance(s3Info['chunkLength'], int)
        self.assertEqual(s3Info['request']['method'], 'PUT')
        self.assertRegex(s3Info['request']['url'], s3Regex)
        self.assertEqual(s3Info['request']['headers']['x-amz-acl'], 'private')

        # Test resume of a single-chunk upload
        resp = self.request(path='/file/offset', method='GET', user=self.admin,
                            params={'uploadId': resp.json['_id']})
        self.assertStatusOk(resp)
        self.assertEqual(resp.json['method'], 'PUT')
        self.assertTrue('headers' in resp.json)
        self.assertRegex(resp.json['url'], s3Regex)

        # Test finalize for a single-chunk upload
        resp = self.request(
            path='/file/completion', method='POST', user=self.admin,
            params={'uploadId': singleChunkUpload['_id']})
        self.assertStatusOk(resp)
        self.assertEqual(resp.json['size'], 1024)
        self.assertEqual(resp.json['assetstoreId'], str(assetstore['_id']))
        self.assertFalse('s3Key' in resp.json)
        self.assertFalse('relpath' in resp.json)

        file = File().load(resp.json['_id'], force=True)
        self.assertTrue('s3Key' in file)
        self.assertRegex(file['relpath'], '^/bucketname/foo/bar/')

        # Test init for a multi-chunk upload
        params['size'] = 1024 * 1024 * 1024 * 5
        resp = self.request(path='/file', method='POST', user=self.admin, params=params)
        self.assertStatusOk(resp)

        multiChunkUpload = resp.json
        s3Info = multiChunkUpload['s3']
        self.assertEqual(s3Info['chunked'], True)
        self.assertIsInstance(s3Info['chunkLength'], int)
        self.assertEqual(s3Info['request']['method'], 'POST')
        self.assertRegex(s3Info['request']['url'], s3Regex)

        # Test uploading a chunk
        resp = self.request(path='/file/chunk', method='POST',
                            user=self.admin, params={
                                'uploadId': multiChunkUpload['_id'],
                                'offset': 0,
                                'chunk': json.dumps({
                                    'partNumber': 1,
                                    's3UploadId': 'abcd'
                                })
                            })
        self.assertStatusOk(resp)
        self.assertRegex(resp.json['s3']['request']['url'], s3Regex)
        self.assertEqual(resp.json['s3']['request']['method'], 'PUT')

        # We should not be able to call file/offset with multi-chunk upload
        resp = self.request(path='/file/offset', method='GET', user=self.admin,
                            params={'uploadId': multiChunkUpload['_id']})
        self.assertStatus(resp, 400)
        self.assertEqual(resp.json, {
            'type': 'validation',
            'message': 'You should not call requestOffset on a chunked '
                       'direct-to-S3 upload.'
        })

        # Test finalize for a multi-chunk upload
        resp = self.request(
            path='/file/completion', method='POST', user=self.admin,
            params={'uploadId': multiChunkUpload['_id']})
        largeFile = resp.json
        self.assertStatusOk(resp)
        self.assertRegex(resp.json['s3FinalizeRequest']['url'], s3Regex)
        self.assertEqual(resp.json['s3FinalizeRequest']['method'], 'POST')

        # Test init for an empty file (should be no-op)
        params['size'] = 0
        resp = self.request(path='/file', method='POST', user=self.admin, params=params)
        emptyFile = resp.json
        self.assertStatusOk(resp)
        self.assertFalse('behavior' in resp.json)
        self.assertFalse('s3' in resp.json)

        # Test download for an empty file
        resp = self.request(
            path='/file/%s/download' % emptyFile['_id'], user=self.admin, method='GET',
            isJson=False)
        self.assertStatusOk(resp)
        self.assertEqual(self.getBody(resp), '')
        self.assertEqual(resp.headers['Content-Length'], 0)
        self.assertEqual(resp.headers['Content-Disposition'], 'attachment; filename="My File.txt"')

        # Test download of a non-empty file
        resp = self.request(path='/file/%s/download' % largeFile['_id'],
                            user=self.admin, method='GET', isJson=False)
        self.assertStatus(resp, 303)
        self.assertRegex(resp.headers['Location'], s3Regex)

        # Test download of a non-empty file, with Content-Disposition=inline.
        # Expect the special S3 header response-content-disposition.
        params = {'contentDisposition': 'inline'}
        inlineRegex = r'response-content-disposition=inline%3B%20filename%3D%22My%20File.txt%22'
        resp = self.request(
            path='/file/%s/download' % largeFile['_id'], user=self.admin, method='GET',
            isJson=False, params=params)
        self.assertStatus(resp, 303)
        self.assertRegex(resp.headers['Location'], s3Regex)
        self.assertRegex(resp.headers['Location'], inlineRegex)

        # Test download as part of a streaming zip
        @httmock.all_requests
        def s3_pipe_mock(url, request):
            if 's3.amazonaws.com' in url.netloc and url.scheme == 'https':
                return 'dummy file contents'
            else:
                raise Exception('Unexpected url %s' % url)

        with httmock.HTTMock(s3_pipe_mock):
            resp = self.request(
                '/folder/%s/download' % parentFolder['_id'],
                method='GET', user=self.admin, isJson=False)
            self.assertStatusOk(resp)
            zip = zipfile.ZipFile(io.BytesIO(self.getBody(resp, text=False)), 'r')
            self.assertTrue(zip.testzip() is None)

            extracted = zip.read('Public/My File.txt')
            self.assertEqual(extracted, b'dummy file contents')

        # Create a "test" key for importing
        client.put_object(Bucket='bucketname', Key='foo/bar/test', Body=b'')

        # Attempt to import item directly into user; should fail
        resp = self.request(
            '/assetstore/%s/import' % assetstore['_id'], method='POST', params={
                'importPath': '/foo/bar/',
                'destinationType': 'user',
                'destinationId': self.admin['_id']
            }, user=self.admin)
        self.assertStatus(resp, 400)
        self.assertEqual(
            resp.json['message'], 'Keys cannot be imported directly underneath a user.')

        # Import existing data from S3
        resp = self.request('/folder', method='POST', params={
            'parentType': 'folder',
            'parentId': parentFolder['_id'],
            'name': 'import destinaton'
        }, user=self.admin)
        self.assertStatusOk(resp)
        importFolder = resp.json

        resp = self.request(
            '/assetstore/%s/import' % assetstore['_id'], method='POST', params={
                'importPath': '',
                'destinationType': 'folder',
                'destinationId': importFolder['_id'],
            }, user=self.admin)
        self.assertStatusOk(resp)

        # Data should now appear in the tree
        resp = self.request('/folder', user=self.admin, params={
            'parentId': importFolder['_id'],
            'parentType': 'folder'
        })
        self.assertStatusOk(resp)
        children = resp.json
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]['name'], 'foo')

        resp = self.request('/folder', user=self.admin, params={
            'parentId': children[0]['_id'],
            'parentType': 'folder'
        })
        self.assertStatusOk(resp)
        children = resp.json
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]['name'], 'bar')

        resp = self.request('/item', user=self.admin, params={
            'folderId': children[0]['_id']
        })
        self.assertStatusOk(resp)
        self.assertEqual(len(resp.json), 1)
        item = resp.json[0]
        self.assertEqual(item['name'], 'test')
        self.assertEqual(item['size'], 0)

        resp = self.request('/item/%s/files' % item['_id'], user=self.admin)
        self.assertStatusOk(resp)
        self.assertEqual(len(resp.json), 1)
        self.assertFalse('imported' in resp.json[0])
        self.assertFalse('relpath' in resp.json[0])
        file = File().load(resp.json[0]['_id'], force=True)
        self.assertTrue(file['imported'])
        self.assertFalse('relpath' in file)
        self.assertEqual(file['size'], 0)
        self.assertEqual(file['assetstoreId'], assetstore['_id'])
        self.assertTrue(client.get_object(Bucket='bucketname', Key='foo/bar/test') is not None)

        # Deleting an imported file should not delete it from S3
        with unittest.mock.patch('girder.events.daemon.trigger') as daemon:
            resp = self.request('/item/%s' % str(item['_id']), method='DELETE', user=self.admin)
            self.assertStatusOk(resp)
            self.assertEqual(len(daemon.mock_calls), 0)

        # Create the file key in the moto s3 store so that we can test that it gets deleted.
        file = File().load(largeFile['_id'], user=self.admin)
        client.create_multipart_upload(Bucket='bucketname', Key=file['s3Key'])
        client.put_object(Bucket='bucketname', Key=file['s3Key'], Body=b'test')

        # Test delete for a non-empty file
        resp = self.request(path='/file/%s' % largeFile['_id'], user=self.admin, method='DELETE')
        self.assertStatusOk(resp)

        # The file should be gone now
        resp = self.request(
            path='/file/%s/download' % largeFile['_id'], user=self.admin, isJson=False)
        self.assertStatus(resp, 400)
        # The actual delete may still be in the event queue, so we want to
        # check the S3 bucket directly.
        startTime = time.time()
        while True:
            try:
                client.get_object(Bucket='bucketname', Key=file['s3Key'])
            except botocore.exceptions.ClientError:
                break
            if time.time() - startTime > 15:
                break  # give up and fail
            time.sleep(0.1)
        with self.assertRaises(botocore.exceptions.ClientError):
            client.get_object(Bucket='bucketname', Key=file['s3Key'])

        resp = self.request(
            path='/folder/%s' % parentFolder['_id'], method='DELETE', user=self.admin)
        self.assertStatusOk(resp)

    def testMoveBetweenAssetstores(self):
        folder = next(Folder().childFolders(
            self.admin, parentType='user', force=True, filters={
                'name': 'Public'
            }))

        resp = self.request(path='/assetstore', method='GET', user=self.admin)
        self.assertStatusOk(resp)
        fs_assetstore = resp.json[0]

        # Clear any old DB data
        base.dropGridFSDatabase('girder_test_assetstore_move_assetstore')
        params = {
            'name': 'New Name',
            'type': AssetstoreType.GRIDFS,
            'db': 'girder_test_assetstore_move_assetstore'
        }
        resp = self.request(path='/assetstore', method='POST', user=self.admin,
                            params=params)
        self.assertStatusOk(resp)
        gridfs_assetstore = resp.json

        # Upload a file - it should go to the fs assetstore
        uploadData = 'helloworld'
        params = {
            'parentType': 'folder',
            'parentId': folder['_id'],
            'name': 'sample1',
            'size': len(uploadData),
            'mimeType': 'text/plain'
        }
        resp = self.request(
            path='/file', method='POST', user=self.admin, params=params)
        self.assertStatusOk(resp)
        upload = resp.json
        resp = self.request(
            path='/file/chunk', method='POST', user=self.admin, body=uploadData, params={
                'uploadId': upload['_id']
            }, type='text/plain')
        self.assertStatusOk(resp)
        self.assertEqual(resp.json['assetstoreId'], fs_assetstore['_id'])
        uploadedFiles = [resp.json]

        # Upload it again targeting a different assetstore
        params['assetstoreId'] = gridfs_assetstore['_id']
        resp = self.request(
            path='/file', method='POST', user=self.admin, params=params)
        self.assertStatusOk(resp)
        upload = resp.json
        resp = self.request(
            path='/file/chunk', method='POST', user=self.admin, body=uploadData, params={
                'uploadId': upload['_id']
            }, type='text/plain')
        self.assertStatusOk(resp)
        self.assertEqual(resp.json['assetstoreId'], gridfs_assetstore['_id'])
        uploadedFiles.append(resp.json)

        # Replace the first file, directing the replacement to a different
        # assetstore
        replaceParams = {
            'size': len(uploadData),
            'assetstoreId': gridfs_assetstore['_id'],
        }
        resp = self.request(
            path='/file/%s/contents' % uploadedFiles[0]['_id'], method='PUT',
            user=self.admin, params=replaceParams)
        self.assertStatusOk(resp)
        upload = resp.json

        resp = self.request(
            path='/file/chunk', method='POST', user=self.admin, body=uploadData, params={
                'uploadId': upload['_id']
            }, type='text/plain')
        self.assertStatusOk(resp)
        self.assertEqual(resp.json['assetstoreId'], gridfs_assetstore['_id'])
        uploadedFiles[0] = resp.json

        # Move a file from the gridfs assetstore to the filesystem assetstore
        resp = self.request(
            path='/file/%s/move' % uploadedFiles[0]['_id'], method='PUT',
            user=self.admin, params={'assetstoreId': fs_assetstore['_id']})
        self.assertStatusOk(resp)
        self.assertEqual(resp.json['assetstoreId'], fs_assetstore['_id'])
        uploadedFiles[0] = resp.json

        # Doing it again shouldn't change it.
        resp = self.request(
            path='/file/%s/move' % uploadedFiles[0]['_id'], method='PUT',
            user=self.admin, params={'assetstoreId': fs_assetstore['_id']})
        self.assertStatusOk(resp)
        self.assertEqual(resp.json['assetstoreId'], fs_assetstore['_id'])
        uploadedFiles[0] = resp.json

        # We should be able to move it back
        resp = self.request(
            path='/file/%s/move' % uploadedFiles[0]['_id'], method='PUT',
            user=self.admin, params={'assetstoreId': gridfs_assetstore['_id']})
        self.assertStatusOk(resp)
        self.assertEqual(resp.json['assetstoreId'], gridfs_assetstore['_id'])
        uploadedFiles[0] = resp.json

        # Test moving a file of zero length
        params['size'] = 0
        resp = self.request(
            path='/file', method='POST', user=self.admin, params=params)
        self.assertStatusOk(resp)
        uploadedFiles.append(resp.json)

        resp = self.request(
            path='/file/%s/move' % uploadedFiles[2]['_id'], method='PUT',
            user=self.admin, params={'assetstoreId': fs_assetstore['_id']})
        self.assertStatusOk(resp)
        self.assertEqual(resp.json['assetstoreId'], fs_assetstore['_id'])
        uploadedFiles[2] = resp.json

        # Test preventing the move via an event
        def stopMove(event):
            event.preventDefault()

        events.bind('model.upload.movefile', 'assetstore_test', stopMove)
        try:
            resp = self.request(
                path='/file/%s/move' % uploadedFiles[0]['_id'], method='PUT',
                user=self.admin, params={'assetstoreId': fs_assetstore['_id']},
                isJson=False)
            self.assertFalse('Move should have been prevented')
        except AssertionError as exc:
            self.assertIn('could not be moved to assetstore', str(exc))
        events.unbind('model.upload.movefile', 'assetstore_test')

        # Test files big enough to be multi-chunk
        chunkSize = Upload()._getChunkSize()
        data = io.BytesIO(b' ' * chunkSize * 2)
        uploadedFiles.append(Upload().uploadFromFile(
            data, chunkSize * 2, 'sample', parentType='folder',
            parent=folder, assetstore=fs_assetstore))
        resp = self.request(
            path='/file/%s/move' % uploadedFiles[3]['_id'], method='PUT',
            user=self.admin, params={'assetstoreId': gridfs_assetstore['_id']})
        self.assertStatusOk(resp)
        self.assertEqual(resp.json['assetstoreId'], gridfs_assetstore['_id'])
        uploadedFiles[3] = resp.json

        # Test progress
        size = chunkSize * 2
        data = io.BytesIO(b' ' * size)
        upload = Upload().uploadFromFile(
            data, size, 'progress', parentType='folder',
            parent=folder, assetstore=fs_assetstore)
        params = {
            'assetstoreId': gridfs_assetstore['_id'],
            'progress': True
        }
        resp = self.request(
            path='/file/%s/move' % upload['_id'], method='PUT',
            user=self.admin, params=params)
        self.assertStatusOk(resp)
        self.assertEqual(resp.json['assetstoreId'], gridfs_assetstore['_id'])

        resp = self.request(
            path='/notification/stream', method='GET', user=self.admin,
            isJson=False, params={'timeout': 1})
        messages = self.getSseMessages(resp)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]['type'], 'progress')
        self.assertEqual(messages[0]['data']['current'], size)

        # Test moving imported file

        # Create assetstore to import file into
        params = {
            'name': 'ImportTest',
            'type': AssetstoreType.FILESYSTEM,
            'root': os.path.join(fs_assetstore['root'], 'import')
        }
        resp = self.request(path='/assetstore', method='POST', user=self.admin,
                            params=params)
        self.assertStatusOk(resp)
        import_assetstore = resp.json

        # Import file
        params = {
            'importPath': os.path.join(ROOT_DIR, 'tests', 'cases', 'py_client',
                                       'testdata', 'world.txt'),
            'destinationType': 'folder',
        }

        Assetstore().importData(
            import_assetstore, parent=folder, parentType='folder', params=params,
            progress=ProgressContext(False), user=self.admin, leafFoldersAsItems=False)

        file = path_util.lookUpPath('/user/admin/Public/world.txt/world.txt',
                                    self.admin)['document']

        # Move file
        params = {
            'assetstoreId': fs_assetstore['_id'],
        }
        resp = self.request(
            path='/file/%s/move' % file['_id'], method='PUT',
            user=self.admin, params=params)
        self.assertStatusOk(resp)
        self.assertEqual(resp.json['assetstoreId'], fs_assetstore['_id'])

        # Check that we can still download the file
        resp = self.request(
            path='/file/%s/download' % file['_id'], user=self.admin, isJson=False)
        self.assertStatusOk(resp)

    def testUnknownAssetstoreType(self):
        assetstore = Assetstore().save({'name': 'Sample', 'type': 'unknown'}, validate=False)
        with self.assertRaises(GirderException):
            assetstore_utilities.getAssetstoreAdapter(assetstore)
        Assetstore().addComputedInfo(assetstore)
        self.assertEqual(assetstore['capacity']['total'], None)
