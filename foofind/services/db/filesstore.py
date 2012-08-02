# -*- coding: utf-8 -*-
import pymongo, logging, time, trace
from collections import defaultdict, OrderedDict
from foofind.utils import hex2mid, end_request, u
from foofind.utils.async import MultiAsync
from foofind.services.extensions import cache
from threading import Lock, Event
from itertools import permutations
from datetime import datetime

class MongoDocument(dict):
    conn_dict = None
    uri = None
    @property
    def conn(self):
        return self.conn_dict(self.uri)

class BogusMongoException(Exception):
    pass

class BongoContext(object):
    def __init__(self, bongo, usemaster=False):
        self.bongo = bongo
        self.uri, self.conn = bongo.urimaster if usemaster else bongo.uriconn

    def __enter__(self):
        return self

    def __exit__(self, t, value, traceback):
        if t:
            if isinstance(t, pymongo.connection.AutoReconnect):
                logging.warn("Autorreconnection throwed by MongoDB server data: %s." % self.uri)
                self.bongo.invalidate(self.uri)
                return True
            elif isinstance(t, pymongo.connection.ConnectionFailure):
                logging.exception("Error accessing to MongoDB server data: %s." % self.uri)
                self.bongo.invalidate(self.uri, True)
                return True

class Bongo(object):
    '''
    Clase para gestionar conexiones a Mongos que funcionan rematadamente mal.
    '''
    def __init__(self, server, pool_size, network_timeout):
        self.info = server
        self.connections = OrderedDict((
            ("mongodb://%(rip)s:%(rp)d" % server, None),
            ("mongodb://%(ip)s:%(p)d" % server, None)
            ))
        self.connections_access = {uri:Event() for uri in self.connections.iterkeys()}
        self._pool_size = pool_size
        self._network_timeout = network_timeout
        self._will_reconnect = set()
        self._will_reconnect_lock = Lock()
        self.reconnect()

    def invalidate(self, uri, cancelConnection=False):
        cache.cacheme = False
        self.connections_access[uri].clear()

        if cancelConnection:
            self.connections[uri].disconnect()
            self.connections[uri] = None
        else:
            self._will_reconnect_lock.acquire()
            self._will_reconnect.add(uri)
            self._will_reconnect_lock.release()

    def reconnect(self):
        for uri, conn in self.connections.iteritems():
            if not self.connections_access[uri].is_set():
                try:
                    self.connections[uri] = pymongo.Connection(
                        uri,
                        max_pool_size = self._pool_size,
                        network_timeout = self._network_timeout
                        )
                    self.connections_access[uri].set()
                    logging.info("Connection has been reestablished with %s" % uri)
                except:
                    logging.warn("Unable to reconnect with %s" % uri)

        to_reconnect = tuple(self._will_reconnect)

        reconnected = []
        for uri in to_reconnect:
            try:
                self.connections[uri].server_info()
                logging.info("Connection check: OK %s" % uri)
                reconnected.append(uri)
                self.connections_access[uri].set()
            except pymongo.connection.AutoReconnect as e:
                logging.warn("Connection check: Autorreconnect %s" % uri)
            except pymongo.connection.ConnectionFailure as e:
                logging.warn("Connection check: ConnectionFailure %s" % uri)
                self.connections[uri] = None

        self._will_reconnect_lock.acquire()
        self._will_reconnect.difference_update(reconnected)
        self._will_reconnect_lock.release()

    @property
    def uriconn(self):
        for uri, conn in self.connections.iteritems():
            if self.connections_access[uri].is_set():
                return uri, conn
        cache.cacheme = False
        raise BogusMongoException("%s has no available connections" % self.__str__())

    @property
    def urimaster(self):
        return self.connections.items()[-1]

    @property
    def conn(self):
        return self.uriconn[1]

    @property
    def master(self):
        return self.connections.values()[-1]

    @property
    def context(self):
        return BongoContext(self)

    @property
    def contextmaster(self):
        return BongoContext(self, True)

    def __str__(self):
        return "<Bongo %s>" %  " ".join(self.connections.iterkeys())

    def __repr__(self):
        return self.__str__()

class FilesStore(object):

    '''
    Clase para acceder a los datos de los ficheros.
    '''
    BogusMongoException = BogusMongoException

    def __init__(self):
        '''
        Inicialización de la clase.
        '''
        self.max_pool_size = 0
        self.server_conn = None
        self.servers_conn = {}
        self.current_server = None

    def init_app(self, app):
        '''
        Inicializa la clase con la configuración de la aplicación.

        @param app: Aplicación de Flask.
        '''
        self.max_pool_size = app.config["DATA_SOURCE_MAX_POOL_SIZE"]
        self.get_files_timeout = app.config["GET_FILES_TIMEOUT"]

        self.server_conn = pymongo.Connection(app.config["DATA_SOURCE_SERVER"], slave_okay=True, max_pool_size=self.max_pool_size)
        self.load_servers_conn()

    def load_servers_conn(self):
        '''
        Configura las conexiones a las bases de datos indicadas en la tabla server de la bd principal.
        Puede ser llamada para actualizar las conexiones si se actualiza la tabla server.
        '''
        for bongo in self.servers_conn.itervalues():
            bongo.reconnect()
        for server in self.server_conn.foofind.server.find().sort([("lt",-1)]):
            sid = server["_id"]

            if not sid in self.servers_conn:
                self.servers_conn[sid] = Bongo(server, self.max_pool_size, self.get_files_timeout)
        self.current_server = max(
            self.servers_conn.itervalues(),
            key=lambda x: x.info["lt"] if "lt" in x.info else datetime.min)

    def get_files(self, ids, servers_known = False, bl = 0):
        '''
        Devuelve los datos de los ficheros correspondientes a los ids
        dados en formato hexadecimal.

        @param ids: Lista de identificadores de los ficheros a recuperar. Si server_known es False, es una lista de cadenas. Si server_known es True, es una lista de tuplas, que incluyen el identificador del fichero y el número de servidor.
        @param servers_known: Indica si la lista de identificadores incluye el servidor donde se encuentra el fichero.


        @type bl: int o None
        @param bl: valor de bl para buscar, None para no restringir

        @rtype generator
        @return Generador con los documentos de ficheros
        '''
        files_count = len(ids)
        if files_count == 0: return ()

        sids = defaultdict(list)
        # si conoce los servidores en los que están los ficheros,
        # se analiza ids como un iterable (id, servidor, ...)
        # y evitamos buscar los servidores
        if servers_known:
            for x in ids:
                sids[x[1]].append(hex2mid(x[0]))
        else:
            # averigua en qué servidor está cada fichero
            nindir = self.server_conn.foofind.indir.find({"_id": {"$in": [hex2mid(fid) for fid in ids]}})
            for ind in nindir:
                if "t" in ind: # si apunta a otro id, lo busca en vez del id dado
                    sids[ind["s"]].append(ind["t"])
                else:
                    sids[ind["s"]].append(ind["_id"])
            self.server_conn.end_request()

        if len(sids) == 0: # Si no hay servidores, no hay ficheros
            return tuple()
        elif len(sids) == 1: # Si todos los ficheros pertenecen al mismo servidor, evita MultiAsync
            sid = sids.keys()[0]
            with self.servers_conn[sid].context as context:
                return end_request(context.conn.foofind.foo.find(
                        {"_id":{"$in": sids[sid]}} if bl is None else
                        {"_id":{"$in": sids[sid]},"bl":bl}))

        # función que recupera ficheros
        def get_server_files(async, sid, ids):
            '''
                Recupera los datos de los ficheros con los ids dados del
                servidor mongo indicado por sid y los devuelve a través
                del objeto async.
            '''
            with self.servers_conn[sid].context as context:
                async.return_value(end_request(context.conn.foofind.foo.find(
                    {"_id": {"$in": ids}}
                    if bl is None else
                    {"_id": {"$in": ids},"bl":bl})))

        # obtiene la información de los ficheros de cada servidor
        return MultiAsync(get_server_files, sids.items(), files_count).get_values(self.get_files_timeout)

    def get_file(self, fid, sid=None, bl=0):
        '''
        Obtiene un fichero del servidor

        @type fid: str
        @param fid: id de fichero en hexadecimal

        @type sid: str
        @param sid: id del servidor

        @type bl: int o None
        @param bl: valor de bl para buscar, None para no restringir

        @rtype mongodb document
        @return Documento del fichero
        '''
        mfid = hex2mid(fid)
        if sid is None:
            # averigua en qué servidor está el fichero
            ind = self.server_conn.foofind.indir.find_one({"_id":mfid})
            if ind is None: return None
            if "t" in ind: mfid = ind["t"]
            sid = ind["s"]
            self.server_conn.end_request()

        with self.servers_conn[sid].context as context:
            return end_request(context.conn.foofind.foo.find_one(
                {"_id":mfid} if bl is None else
                {"_id":mfid,"bl":bl}), context.conn)

    def get_newid(self, oldid):
        '''
        Traduce un ID antiguo (secuencial) al nuevo formato.

        @type oldid: digit-str o int
        @param oldid: Id antiguo, en string de números o entero.

        @rtype None o ObjectID
        @return None si no es válido o no encontrado, o id de mongo.
        '''
        if isinstance(oldid, basestring):
            if not oldid.isdigit():
                return None
        with self.servers_conn[1.0].context as context:
            doc = context.conn.foofind.foo.find_one({"i":int(oldid)})
            context.conn.end_request()
            if doc:
                return doc["_id"]
        return None

    def update_file(self, data, remove=None, direct_connection=False, update_sphinx=True):
        '''
        Actualiza los datos del fichero dado.

        @type data: dict
        @param data: Diccionario con los datos del fichero a guardar. Se debe incluir '_id', y es recomendable incluir 's' por motivos de rendimiento.
        @type remove: iterable
        @param remove: Lista de campos a quitar del registro.
        @type direct_connection: bool
        @param direct_connection: Especifica si se crea una conexión directa, ineficiente e independiente al foo primario.
        @type update_sphinx: bool
        @param update_sphinx: si se especifica bl, de este parámetro depende el si se conecta al sphinx para actualizarlo
        '''
        update = {"$set":data.copy()}
        if remove is not None:
            update["$unset"]=dict()
            for rem in remove:
                del update["$set"][rem]
                update["$unset"][rem]=1

        fid = hex2mid(data["_id"])

        if "s" in data:
            server = data["s"]
        else:
            try:
                server = self.get_file(fid, bl=None)["s"]
            except (TypeError, KeyError) as e:
                logging.error("Se ha intentado actualizar un fichero que no se encuentra en la base de datos", extra=data)
                raise

        if update_sphinx and "bl" in data:
            block_files(mongo_ids=(i["_id"],), block=data["bl"])

        for i in ("_id", "s"):
            if i in update["$set"]:
                del update["$set"][i]

        if direct_connection:
            # TODO: update cache
            with self.servers_conn[server].contextmaster as context:
                context.conn.foofind.foo.update({"_id":fid}, update)
                context.conn.end_request()
        else:
            #TODO(felipe): implementar usando el EventManager
            raise NotImplemented("No se ha implementado una forma eficiente de actualizar un foo")

    def count_files(self):
        '''
        Cuenta los ficheros totales indexados
        '''
        count = self.server_conn.foofind.server.group(None, None, {"c":0}, "function(o,p) { p.c += o.c; }")
        return end_request(count[0]['c'] if count else 0, self.server_conn)

    @cache.memoize(timeout=2)
    @cache.fallback(BogusMongoException)
    def get_last_files(self, n=25):
        '''
        Obtiene los últimos 25 ficheros indexados
        '''
        with self.current_server.context as context:
            return end_request(
                tuple(context.conn.foofind.foo.find({"bl":0}).sort([("$natural",-1)]).limit(n)),
                context.conn)

    def remove_source_by_id(self, sid):
        '''
        Borra un origen con su id

        @type sid: int o float
        @param sid: identificador del origen
        '''
        self.request_conn.foofind.source.remove({"_id":sid})

    @cache.memoize()
    def get_source_by_id(self, source):
        '''
        Obtiene un origen a través del id

        @type source: int o float
        @param source: identificador del source

        @rtype dict o None
        @return origen o None

        '''
        return end_request(
            self.server_conn.foofind.source.find_one({"_id":source}),
            self.server_conn)

    _sourceParse = { # Parseo de datos para base de datos
        "_id":lambda x:int(float(x)),
        "crbl":lambda x:int(float(x)),
        "g":lambda x: [i.strip() for i in x.split(",") if i.strip()] if isinstance(x, basestring) else x,
        "*":u
        }
    def update_source(self, data, remove=None):
        '''
        Actualiza los datos del origen dado.

        @type data: dict
        @param data: Diccionario con los datos del fichero a guardar. Se debe incluir '_id'.
        @type remove: iterable
        @param remove: Lista de campos a quitar del registro.
        '''

        update = {"$set":data.copy()}
        if remove is not None:
            update["$unset"]=dict()
            for rem in remove:
                del update["$set"][rem]
                update["$unset"][rem]=1

        oid = int(float(data["_id"])) #hex2mid(data["_id"])
        del update["$set"]["_id"]

        parser = self._sourceParse

        update["$set"].update(
            (key, parser[key](value) if key in parser else parser["*"](value))
            for key, value in update["$set"].iteritems())


        self.server_conn.foofind.source.update({"_id":oid}, update)
        self.server_conn.end_request()

    def create_source(self, data):
        '''
        Crea el origen dado.

        @type data: dict
        @param data: Diccionario con los datos del fichero a guardar. Se debe incluir '_id'.
        '''
        parser = self._sourceParse

        doc = {
            key: (parser[key](value) if key in parser else parser["*"](value))
            for key, value in data.iteritems()
            }

        self.server_conn.foofind.source.insert(doc)
        self.server_conn.end_request()

    @cache.memoize()
    def get_sources(self, skip=None, limit=None, blocked=False, group=None, must_contain_all=False):
        '''
        Obtiene los orígenes como generador

        @type skip: int
        @param skip: número de elementos omitidos al inicio, None por defecto.

        @type limit: int
        @param limit: número de elementos máximos que obtener, None por defecto para todos.

        @type blocked: bool o None
        @param blocked: retornar elementos ya procesados, None para obtenerlos todos, False por defecto.

        @type group: basestring, iterable o None
        @param group: basestring para un grupo, si es None, para todos

        @type must_contain_all: bool
        @param must_contain_all: False para encontrar todos los que contengan alguno de los grupos de group, True para encontrar los que contengan todos los groups de group.

        @rtype: MongoDB cursor
        @return: cursor con resultados
        '''
        query = {}
        if blocked == True: query["crbl"] = 1
        elif blocked == False: query["$or"] = {"crbl": { "$exists" : False } }, {"crbl":0}
        if not group is None:
            if isinstance(group, basestring): query["g"] = group
            elif must_contain_all: query["g"] = {"$all": group}
            else: query["g"] = {"$in": group}
        sources = self.server_conn.foofind.source.find(query).sort("d")
        if not skip is None: sources.skip(skip)
        if not limit is None: sources.limit(limit)
        return list(end_request(sources))

    def count_sources(self, blocked=False, group=None, must_contain_all=False, limit=None):
        '''
        Obtiene el número de orígenes

        @type blocked: bool o None
        @param blocked: retornar elementos ya procesados, None para obtenerlos todos, False por defecto.

        @type group: basestring, iterable o None
        @param group: basestring para un grupo, si es None, para todos

        @type must_contain_all: bool
        @param must_contain_all: False para encontrar todos los que contengan alguno de los grupos de group, True para encontrar los que contengan todos los groups de group.

        @rtype integer
        @return Número de orígenes
        '''
        query = {} if limit is None else {"limit":limit}
        if blocked == True: query["crbl"] = 1
        elif blocked == False: query["$or"] = {"crbl": { "$exists" : False } }, {"crbl":0}
        if not group is None:
            if isinstance(group, basestring): query["g"] = group
            elif must_contain_all: query["g"] = {"$all": group}
            else: query["g"] = {"$in": group}

        return end_request(self.server_conn.foofind.source.find(query).count(True), self.server_conn)

    @cache.memoize()
    def get_sources_groups(self):
        '''
        Obtiene los grupos de los orígenes

        @return set de grupos de orígenes
        '''
        return set(j for i in end_request(self.server_conn.foofind.source.find()) if "g" in i for j in i["g"])

    @cache.memoize()
    def get_image_server(self,server):
        '''
        Obtiene el servidor que contiene una imagen
        '''
        return end_request(
            self.server_conn.foofind.serverImage.find_one({"_id":server}),
            self.server_conn)

    @cache.memoize()
    def get_server_stats(self, server):
        '''
        Obtiene las estadisticas del servidor
        '''
        return end_request(
            self.server_conn.foofind.search_stats.find_one({"_id":server}),
            self.server_conn)

    @cache.memoize()
    def get_servers(self):
        '''
        Obtiene informacion de los servidores de datos
        '''
        return list(end_request(self.server_conn.foofind.server.find(), self.server_conn))
