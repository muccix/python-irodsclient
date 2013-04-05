import socket 
import hashlib
import struct
import logging
from os.path import basename, dirname
from os import O_RDONLY, O_WRONLY, O_RDWR

from message import (iRODSMessage, StartupPack, authResponseInp, GenQueryOut, 
    DataObjInp, authRequestOut, KeyValPair, dataObjReadInp, dataObjWriteInp,
    fileLseekInp, fileLseekOut, dataObjCloseInp, ModAVUMetadataInp,
    empty_gen_query_out)
from . import MAX_PASSWORD_LENGTH
from query import Query
from exception import (get_exception_by_code, CAT_NO_ROWS_FOUND, 
    CollectionDoesNotExist, DataObjectDoesNotExist)
from results import ResultSet
from models import (Collection, DataObject, Resource, User, DataObjectMeta, 
    CollectionMeta, ResourceMeta, UserMeta)
from collection import iRODSCollection
from data_object import iRODSDataObject
from api_number import api_number
from meta import iRODSMeta

class iRODSSession(object):
    def __init__(self, *args, **kwargs):
        self.socket = None
        self.authenticated = False
        if args or kwargs:
            self.configure(*args, **kwargs)

    def configure(self, host=None, port=1247, user=None, zone=None, password=None):
        self.host = host
        self.port = port
        self.user = user
        self.zone = zone
        self.password = password    
        self._connect()

    def __del__(self):
        if self.socket:
            self.disconnect()

    def _send(self, message):
        str = message.pack()
        logging.debug(str)
        return self.socket.sendall(str)

    def _recv(self):
        msg = iRODSMessage.recv(self.socket)
        if msg.int_info < 0:
            raise get_exception_by_code(msg.int_info)
        return msg

    def _connect(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        try:
            s.connect((self.host, self.port))
        except socket.error:
            raise Exception("Could not connect to specified host and port")

        self.socket = s
        main_message = StartupPack(self.user, self.zone)
        msg = iRODSMessage(type='RODS_CONNECT', msg=main_message)
        self._send(msg)
        version_msg = self._recv()

    def disconnect(self):
        disconnect_msg = iRODSMessage(type='RODS_DISCONNECT')
        self._send(disconnect_msg)
        self.socket.close()

    def _login(self):
        # authenticate
        auth_req = iRODSMessage(type='RODS_API_REQ', int_info=703)
        self._send(auth_req)

        # challenge
        challenge_msg = self._recv()
        logging.debug(challenge_msg.msg)
        challenge = challenge_msg.get_main_message(authRequestOut).challenge
        padded_pwd = struct.pack("%ds" % MAX_PASSWORD_LENGTH, self.password)
        m = hashlib.md5()
        m.update(challenge)
        m.update(padded_pwd)
        encoded_pwd = m.digest()

        encoded_pwd = encoded_pwd.replace('\x00', '\x01')
        pwd_msg = authResponseInp(response=encoded_pwd, username=self.user)
        pwd_request = iRODSMessage(type='RODS_API_REQ', int_info=704, msg=pwd_msg)
        self._send(pwd_request)

        auth_response = self._recv()
        if auth_response.error:
            raise Exception("Unsuccessful login attempt")
        else:
            self.authenticated = True
            logging.debug("Successful login")

    def get_collection(self, path):
        if not self.authenticated:
            self._login()
        query = self.query(Collection).filter(Collection.name == path)
        results = self.execute_query(query)
        # todo implement this with .one() on query
        if results.length == 1:
            return iRODSCollection(self, results[0])
        else:
            raise CollectionDoesNotExist()

    def get_data_object(self, path):
        if not self.authenticated:
            self._login()
        parent = self.get_collection(dirname(path))
        results = self.query(DataObject)\
            .filter(DataObject.name == basename(path))\
            .filter(DataObject.collection_id == parent.id)\
            .all()
        if results.length == 1:
            return iRODSDataObject(self, parent, results[0])
        else:
            raise DataObjectDoesNotExist()

    def create_data_object(self, path):
        if not self.authenticated:
            self._login()
        message_body = DataObjInp(
            objPath=path,
            createMode=0644,
            openFlags=0,
            offset=0,
            dataSize=-1,
            numThreads=0,
            oprType=0,
            KeyValPair_PI=KeyValPair({'dataType': 'generic'}),
        )
        message = iRODSMessage('RODS_API_REQ', msg=message_body,
            int_info=api_number['DATA_OBJ_CREATE_AN'])
        self._send(message)
        response = self._recv()
        desc = response.int_info
        self.close_file(desc)
        return self.get_data_object(path)

    def open_file(self, path, mode):
        if not self.authenticated:
            self._login()
        message_body = DataObjInp(
            objPath=path,
            createMode=0,
            openFlags=mode,
            offset=0,
            dataSize=-1,
            numThreads=0,
            oprType=0,
            KeyValPair_PI=KeyValPair(),
        )
        message = iRODSMessage('RODS_API_REQ', msg=message_body, 
            int_info=api_number['DATA_OBJ_OPEN_AN'])
        self._send(message)
        response = self._recv()
        return response.int_info

    def read_file(self, desc, size):
        if not self.authenticated:
            self._login()
        message_body = dataObjReadInp(
            l1descInx=desc,
            len=size
        )
        message = iRODSMessage('RODS_API_REQ', msg=message_body,
            int_info=api_number['DATA_OBJ_READ201_AN'])
        self._send(message)
        response = self._recv()
        return response.bs

    def write_file(self, desc, string):
        if not self.authenticated:
            self._login()
        message_body = dataObjWriteInp(
            dataObjInx=desc,
            len=len(string)
        )
        message = iRODSMessage('RODS_API_REQ', msg=message_body,
            bs=string,
            int_info=api_number['DATA_OBJ_WRITE201_AN'])
        self._send(message)
        response = self._recv()
        return response.int_info

    def seek_file(self, desc, offset, whence):
        if not self.authenticated:
            self._login()
        message_body = fileLseekInp(
            fileInx=desc,
            offset=offset,
            whence=whence
        )
        message = iRODSMessage('RODS_API_REQ', msg=message_body,
            int_info=api_number['DATA_OBJ_LSEEK201_AN'])
        self._send(message)
        response = self._recv()
        offset = response.get_main_message(fileLseekOut).offset
        return offset

    def close_file(self, desc):
        if not self.authenticated:
            self._login()
        message_body = dataObjCloseInp(
            l1descInx=desc
        )
        message = iRODSMessage('RODS_API_REQ', msg=message_body,
            int_info=api_number['DATA_OBJ_CLOSE201_AN'])
        self._send(message)
        response = self._recv()

    def unlink_data_object(self, path):
        if not self.authenticated:
            self._login()
        message_body = DataObjInp(
            objPath=path,
            createMode=0,
            openFlags=0,
            offset=0,
            dataSize=-1,
            numThreads=0,
            oprType=0,
            KeyValPair_PI=KeyValPair(),
        )
        message = iRODSMessage('RODS_API_REQ', msg=message_body,
            int_info=api_number['DATA_OBJ_UNLINK_AN'])
        self._send(message)
        response = self._recv()

    @staticmethod
    def _model_class_to_resource_type(model_cls):
        return {
            DataObject: 'd',
            Collection: 'c',
            Resource: 'r',
            User: 'r',
        }[model_cls]

    def get_meta(self, model_cls, path):
        resource_type = self._model_class_to_resource_type(model_cls)
        model = {
            'd': DataObjectMeta,
            'c': CollectionMeta,
            'r': ResourceMeta,
            'u': UserMeta
        }[resource_type]
        conditions = {
            'd': [
                Collection.name == dirname(path), 
                DataObject.name == basename(path)
            ],
            'c': [Collection.name == path],
            'r': [Resource.name == path],
            'u': [User.name == path]
        }[resource_type]
        results = self.query(model.id, model.name, model.value, model.units)\
            .filter(*conditions).all()
        return [iRODSMeta(
            row[model.name], 
            row[model.value], 
            row[model.units],
            id=row[model.id]
        ) for row in results]

    def add_meta(self, model_cls, path, meta):
        resource_type = self._model_class_to_resource_type(model_cls)
        message_body = ModAVUMetadataInp(
            "add",
            "-" + resource_type,
            path,
            meta.name,
            meta.value,
            meta.units
        )
        request = iRODSMessage("RODS_API_REQ", msg=message_body, 
            int_info=api_number['MOD_AVU_METADATA_AN'])
        self._send(request)
        response = self._recv()
        logging.debug(response.int_info)

    def remove_meta(self, model_cls, path, meta):
        resource_type = self._model_class_to_resource_type(model_cls)
        message_body = ModAVUMetadataInp(
            "rm",
            "-" + resource_type,
            path,
            meta.name,
            meta.value,
            meta.units
        )
        request = iRODSMessage("RODS_API_REQ", msg=message_body, 
            int_info=api_number['MOD_AVU_METADATA_AN'])
        self._send(request)
        response = self._recv()
        logging.debug(response.int_info)

    def copy_meta(self, src_model_cls, dest_model_cls, src, dest):
        src_resource_type = self._model_class_to_resource_type(src_model_cls)
        dest_resource_type = self._model_class_to_resource_type(dest_model_cls)
        message_body = ModAVUMetadataInp(
            "cp",
            "-" + src_resource_type,
            "-" + dest_resource_type,
            src,
            dest
        )
        request = iRODSMessage("RODS_API_REQ", msg=message_body, 
            int_info=api_number['MOD_AVU_METADATA_AN'])
        self._send(request)
        response = self._recv()
        logging.debug(response.int_info)
        
    def query(self, *args):
        return Query(self, *args)

    def execute_query(self, query):
        if not self.authenticated:
            self._login()
        message_body = query._message()
        message = iRODSMessage('RODS_API_REQ', msg=message_body, int_info=702)
        self._send(message)
        try:
            result_message = self._recv()
            results = result_message.get_main_message(GenQueryOut)
            result_set = ResultSet(results)
        except CAT_NO_ROWS_FOUND:
            result_set = ResultSet(empty_gen_query_out(query.columns.keys())) 
        return result_set
