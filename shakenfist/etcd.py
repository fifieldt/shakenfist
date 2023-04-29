from collections import defaultdict
from etcd3gw.client import Etcd3Client
from etcd3gw.exceptions import InternalServerError
from etcd3gw.lock import Lock
from etcd3gw.utils import _encode, _increment_last_byte
import json
import os
import psutil
import re
import requests
from shakenfist_utilities import (logs, random as util_random)
import threading
import time

from shakenfist import baseobject
from shakenfist.config import config
from shakenfist import exceptions
from shakenfist.tasks import QueueTask, FetchBlobTask


LOG, _ = logs.setup(__name__)
LOCK_PREFIX = '/sflocks'


# NOTE(mikal): it is a limitation of the client that you can't interleave read
# operations -- for example, if you're reading one item at a time from an
# iterator and you yield a result that might cause the caller to want to read
# something else in the same thread, then the etcd client gets confused and
# instead returns the data from the iterator. For now we just jump through some
# hoops to try and ensure that doesn't happen, but we should probably be better
# than that.


class WrappedEtcdClient(Etcd3Client):
    def __init__(self, host=None, port=2379, protocol='http',
                 ca_cert=None, cert_key=None, cert_cert=None, timeout=None,
                 api_path='/v3beta/'):
        if not host:
            host = config.ETCD_HOST

        # Work around https://opendev.org/openstack/etcd3gw/commit/7a1a2b5a672605ae549c73ed18302b7abd9e0e30
        # making things not work for us.
        if api_path == '/v3alpha':
            raise Exception('etcd3 v3alpha endpoint is known not to work')

        # Cache config options so we can reuse them when we rebuild connections.
        self.ca_cert = ca_cert
        self.cert_key = cert_key
        self.cert_cert = cert_cert
        self.timeout = timeout

        LOG.info('Building new etcd connection')
        return super(WrappedEtcdClient, self).__init__(
            host=host, port=port, protocol=protocol, ca_cert=ca_cert,
            cert_key=cert_key, cert_cert=cert_cert, timeout=timeout,
            api_path=api_path)

    # Replace the upstream implementation with one which allows for limits on range
    # queries instead of just erroring out for big result sets.
    def get_prefix(self, key_prefix, sort_order=None, sort_target=None, limit=0):
        """Get a range of keys with a prefix.

        :param sort_order: 'ascend' or 'descend' or None
        :param key_prefix: first key in range

        :returns: sequence of (value, metadata) tuples
        """
        return self.get(key_prefix,
                        metadata=True,
                        range_end=_encode(_increment_last_byte(key_prefix)),
                        sort_order=sort_order,
                        sort_target=sort_target,
                        limit=limit)

    # Wrap post() to retry on errors. These errors are caused by our long lived
    # connections sometimes being dropped.
    def post(self, *args, **kwargs):
        try:
            return super(WrappedEtcdClient, self).post(*args, **kwargs)
        except Exception as e:
            LOG.info('Retrying after receiving etcd error: %s' % e)

            self.session = requests.Session()
            if self.timeout is not None:
                self.session.timeout = self.timeout
            if self.ca_cert is not None:
                self.session.verify = self.ca_cert
            if self.cert_cert is not None and self.cert_key is not None:
                self.session.cert = (self.cert_cert, self.cert_key)
            return super(WrappedEtcdClient, self).post(*args, **kwargs)


# This module stores some state in thread local storage.
local = threading.local()
local.sf_etcd_client = None


def get_etcd_client():
    c = getattr(local, 'sf_etcd_client', None)
    if not c:
        c = local.sf_etcd_client = WrappedEtcdClient()

    # Test the connection
    try:
        c.status()
    except Exception as e:
        LOG.info('Rebuilding etcd connection due to error on status check: %s' % e)
        c = local.sf_etcd_client = WrappedEtcdClient()

    return c


# This read only cache is thread local, a bit like Flask's request object. Given
# this is a read only cache, once you have set one of these up any attempt to
# change or lock data will also result in an exception being raised. This is
# solely about reducing the load on etcd for read only operations.
#
# There is one exception here. I think it is safe to enqueue work items while
# using one of these caches, so it is possible to write a loop which does the
# expensive analysis of state while using one of these caches, and then
# enqueues work to change the database while a cache is not being used.
local.sf_etcd_statistics = defaultdict(int)


def read_only_cache():
    return getattr(local, 'sf_read_only_etcd_cache', None)


def get_statistics():
    return dict(local.sf_etcd_statistics)


def reset_statistics():
    local.sf_etcd_statistics = defaultdict(int)


def _record_uncached_read(path):
    local.sf_etcd_statistics[path] += 1


class ThreadLocalReadOnlyCache():
    def __init__(self):
        if read_only_cache():
            raise exceptions.PreExistingReadOnlyCache('Cache already setup')
        self.prefixes = []

    def __enter__(self):
        self.cache = {}
        local.sf_read_only_etcd_cache = self
        return self

    def __exit__(self, *args):
        local.sf_read_only_etcd_cache = None

    def _cached(self, key):
        for p in self.prefixes:
            if key.startswith(p):
                return True
        return False

    def _find_prefix(self, key):
        # Special cases for namespaces, nodes, and metrics
        for special in ['namespace', 'node', 'metrics']:
            if key.startswith('/sf/%s' % special):
                return '/sf/%s' % special
            if key.startswith('/sf/attribute/%s' % special):
                return '/sf/attribute/%s' % special

        uuid_regex = re.compile('.{8}-.{4}-.{4}-.{4}-.{12}')

        keys = key.split('/')
        while keys:
            if uuid_regex.match(keys.pop()):
                return '/'.join(keys)
        raise ValueError('Attempt to cache etcd key without a UUID: %s' % key)

    def _cache_prefix(self, prefix):
        client = get_etcd_client()
        start_time = time.time()
        for data, metadata in client.get_prefix(prefix):
            self.cache[metadata['key'].decode('utf-8')] = json.loads(data)
        if config.EXCESSIVE_ETCD_CACHE_LOGGING:
            LOG.info('Populating thread local etcd cache took %.02f seconds '
                     'and cached %d keys from %s' % (
                         time.time() - start_time, len(self.cache), prefix))
        self.prefixes.append(prefix)

    def get(self, key):
        if not self._cached(key):
            self._cache_prefix(self._find_prefix(key))
        return self.cache.get(key)

    def get_prefix(self, prefix):
        if not self._cached(prefix):
            self._cache_prefix(prefix)
        for key in self.cache.copy().keys():
            if key.startswith(prefix):
                yield (key, self.cache[key])


def retry_etcd_forever(func):
    """Retry the Etcd server forever.

    If the DB is unable to process the request then SF cannot operate,
    therefore wait until it comes back online. If the DB falls out of sync with
    the system then we will have bigger problems than a small delay.

    If the etcd server is not running, then a ConnectionFailedError exception
    will occur. This is deliberately allowed to cause an SF daemon failure to
    bring attention to the deeper problem.
    """
    def wrapper(*args, **kwargs):
        count = 0
        while True:
            try:
                return func(*args, **kwargs)
            except InternalServerError as e:
                LOG.error('Etcd3gw Internal Server Error: %s' % e)
            time.sleep(count/10.0)
            count += 1
    return wrapper


class ActualLock(Lock):
    def __init__(self, objecttype, subtype, name, ttl=120,
                 client=None, timeout=120, log_ctx=LOG,
                 op=None):
        if read_only_cache():
            raise exceptions.ForbiddenWhileUsingReadOnlyCache(
                'You cannot lock while using a read only cache')

        self.path = _construct_key(objecttype, subtype, name)
        super(ActualLock, self).__init__(self.path, ttl=ttl, client=client)

        self.objecttype = objecttype
        self.objectname = name
        self.timeout = timeout
        self.operation = op

        self.log_ctx = log_ctx.with_fields({
            'lock': self.path,
            'node': config.NODE_NAME,
            'pid': os.getpid(),
            'operation': self.operation})

        # We override the UUID of the lock with something more helpful to debugging
        self._uuid = json.dumps(
            {
                'node': config.NODE_NAME,
                'pid': os.getpid(),
                'operation': self.operation
            },
            indent=4, sort_keys=True)

        # We also override the location of the lock so that we're in our own spot
        self.key = LOCK_PREFIX + self.path

    @retry_etcd_forever
    def get_holder(self):
        value = get_etcd_client().get(
            self.key, metadata=True)
        if value is None or len(value) == 0:
            return None, NotImplementedError

        if not value[0][0]:
            return None, None

        d = json.loads(value[0][0])
        return d['node'], d['pid']

    def __enter__(self):
        start_time = time.time()
        slow_warned = False
        threshold = int(config.SLOW_LOCK_THRESHOLD)

        while time.time() - start_time < self.timeout:
            res = self.acquire()
            if res:
                duration = time.time() - start_time
                if duration > threshold:
                    self.log_ctx.with_fields({
                        'duration': duration}).info('Acquired lock, but it was slow')
                else:
                    self.log_ctx.debug('Acquired lock')
                return self

            duration = time.time() - start_time
            if (duration > threshold and not slow_warned):
                node, pid = self.get_holder()
                self.log_ctx.with_fields({'duration': duration,
                                          'threshold': threshold,
                                          'holder-pid': pid,
                                          'holder-node': node,
                                          'requesting-op': self.operation,
                                          }).info('Waiting to acquire lock')
                slow_warned = True

            time.sleep(1)

        duration = time.time() - start_time

        node, pid = self.get_holder()
        self.log_ctx.with_fields({'duration': duration,
                                  'holder-pid': pid,
                                  'holder-node': node,
                                  'requesting-op': self.operation,
                                  }).info('Failed to acquire lock')

        raise exceptions.LockException(
            'Cannot acquire lock %s, timed out after %.02f seconds'
            % (self.name, self.timeout))

    def __exit__(self, _exception_type, _exception_value, _traceback):
        if not self.release():
            locks = list(get_all(LOCK_PREFIX, None))
            self.log_ctx.with_fields({'locks': locks,
                                      'key': self.name,
                                      }).error('Cannot release lock')
            raise exceptions.LockException(
                'Cannot release lock: %s' % self.name)
        self.log_ctx.debug('Released lock')


def get_lock(objecttype, subtype, name, ttl=60, timeout=10, log_ctx=LOG,
             op=None):
    """Retrieves an etcd lock object. It is not locked, to lock use acquire().

    The returned lock can be used as a context manager, with the lock being
    acquired on entry and released on exit. Note that the lock acquire process
    will have no timeout.
    """
    # FIXME(mikal): excluded from using the thread local etcd client because
    # it is causing locking errors for reasons that are not currently clear to
    # me.
    return ActualLock(objecttype, subtype, name, ttl=ttl,
                      client=WrappedEtcdClient(),
                      log_ctx=log_ctx, timeout=timeout, op=op)


def refresh_lock(lock, log_ctx=LOG):
    if read_only_cache():
        raise exceptions.ForbiddenWhileUsingReadOnlyCache(
            'You cannot hold locks while using a read only cache')

    if not lock.is_acquired():
        log_ctx.with_fields({'lock': lock.name}).info(
            'Attempt to refresh an expired lock')
        raise exceptions.LockException(
            'The lock on %s has expired.' % lock.path)

    lock.refresh()
    log_ctx.with_fields({'lock': lock.name}).debug('Refreshed lock')


@retry_etcd_forever
def clear_stale_locks():
    # Remove all locks held by former processes on this node. This is required
    # after an unclean restart, otherwise we need to wait for these locks to
    # timeout and that can take a long time.
    if read_only_cache():
        raise exceptions.ForbiddenWhileUsingReadOnlyCache(
            'You cannot clear locks while using a read only cache')

    client = get_etcd_client()

    for data, metadata in client.get_prefix(
            LOCK_PREFIX + '/', sort_order='ascend', sort_target='key'):
        lockname = str(metadata['key']).replace(LOCK_PREFIX + '/', '')
        holder = json.loads(data)
        node = holder['node']
        pid = int(holder['pid'])

        if node == config.NODE_NAME and not psutil.pid_exists(pid):
            client.delete(metadata['key'])
            LOG.with_fields({'lock': lockname,
                             'old-pid': pid,
                             'old-node': node,
                             }).warning('Removed stale lock')


@retry_etcd_forever
def get_existing_locks():
    key_val = {}
    for value in get_etcd_client().get_prefix(LOCK_PREFIX + '/'):
        key_val[value[1]['key'].decode('utf-8')] = json.loads(value[0])
    return key_val


def _construct_key(objecttype, subtype, name):
    if subtype and name:
        return '/sf/%s/%s/%s' % (objecttype, subtype, name)
    if name:
        return '/sf/%s/%s' % (objecttype, name)
    if subtype:
        return '/sf/%s/%s/' % (objecttype, subtype)
    return '/sf/%s/' % objecttype


class JSONEncoderCustomTypes(json.JSONEncoder):
    def default(self, obj):
        if QueueTask.__subclasscheck__(type(obj)):
            return obj.obj_dict()
        if type(obj) is baseobject.State:
            return obj.obj_dict()
        return json.JSONEncoder.default(self, obj)


@retry_etcd_forever
def put(objecttype, subtype, name, data, ttl=None):
    # Its ok to create events while using a read only cache
    if read_only_cache() and not objecttype.startswith('event/'):
        raise exceptions.ForbiddenWhileUsingReadOnlyCache(
            'You cannot change data while using a read only cache')

    path = _construct_key(objecttype, subtype, name)
    encoded = json.dumps(data, indent=4, sort_keys=True,
                         cls=JSONEncoderCustomTypes)
    get_etcd_client().put(path, encoded, lease=None)


@retry_etcd_forever
def create(objecttype, subtype, name, data, ttl=None):
    if read_only_cache():
        raise exceptions.ForbiddenWhileUsingReadOnlyCache(
            'You cannot change data while using a read only cache')

    path = _construct_key(objecttype, subtype, name)
    encoded = json.dumps(data, indent=4, sort_keys=True,
                         cls=JSONEncoderCustomTypes)
    return get_etcd_client().create(path, encoded, lease=None)


@retry_etcd_forever
def get(objecttype, subtype, name):
    path = _construct_key(objecttype, subtype, name)

    cache = read_only_cache()
    if cache:
        return cache.get(path)
    _record_uncached_read(path)

    value = get_etcd_client().get(path, metadata=True)
    if value is None or len(value) == 0:
        return None
    return json.loads(value[0][0])


@retry_etcd_forever
def get_all(objecttype, subtype, prefix=None, sort_order=None, limit=0):
    path = _construct_key(objecttype, subtype, prefix)

    cache = read_only_cache()
    if cache:
        for key, value in cache.get_prefix(path):
            yield key, value
    else:
        _record_uncached_read(path)
        for data, metadata in get_etcd_client().get_prefix(
                path, sort_order=sort_order, sort_target='key', limit=limit):
            yield str(metadata['key'].decode('utf-8')), json.loads(data)


@retry_etcd_forever
def get_all_dict(objecttype, subtype=None, sort_order=None, limit=0):
    path = _construct_key(objecttype, subtype, None)
    key_val = {}

    cache = read_only_cache()
    if cache:
        for key, value in cache.get_prefix(path):
            key_val[key] = value
    else:
        _record_uncached_read(path)
        for value in get_etcd_client().get_prefix(
                path, sort_order=sort_order, sort_target='key', limit=limit):
            key_val[value[1]['key'].decode('utf-8')] = json.loads(value[0])

    return key_val


@retry_etcd_forever
def delete(objecttype, subtype, name):
    if read_only_cache():
        raise exceptions.ForbiddenWhileUsingReadOnlyCache(
            'You cannot change data while using a read only cache')

    path = _construct_key(objecttype, subtype, name)
    get_etcd_client().delete(path)


@retry_etcd_forever
def delete_all(objecttype, subtype):
    if read_only_cache():
        raise exceptions.ForbiddenWhileUsingReadOnlyCache(
            'You cannot change data while using a read only cache')

    path = _construct_key(objecttype, subtype, None)
    get_etcd_client().delete_prefix(path)


def enqueue(queuename, workitem, delay=0):
    entry_time = time.time() + delay
    jobname = '%s-%s' % (entry_time, util_random.random_id())
    put('queue', queuename, jobname, workitem)
    LOG.with_fields({'jobname': jobname,
                        'queuename': queuename,
                        'workitem': workitem,
                        }).info('Enqueued workitem')


def _all_subclasses(cls):
    all = cls.__subclasses__()
    for sc in cls.__subclasses__():
        all += _all_subclasses(sc)
    return all


def _find_class(task_item):
    if not isinstance(task_item, dict):
        return task_item

    item = task_item
    for task_class in _all_subclasses(QueueTask):
        if task_class.name() and task_item.get('task') == task_class.name():
            del task_item['task']
            # This is where new QueueTask subclass versions should be handled
            del task_item['version']
            item = task_class(**task_item)
            break

    return item


def decodeTasks(obj):
    if not isinstance(obj, dict):
        return obj

    if 'tasks' in obj:
        task_list = []
        for task_item in obj['tasks']:
            task_list.append(_find_class(task_item))
        return {'tasks': task_list}

    if 'task' in obj:
        return _find_class(obj)

    return obj


@retry_etcd_forever
def dequeue(queuename):
    if read_only_cache():
        raise exceptions.ForbiddenWhileUsingReadOnlyCache(
            'You cannot consume queue work items while using a read only cache')

    queue_path = _construct_key('queue', queuename, None)
    client = get_etcd_client()

    # NOTE(mikal): limit is here to stop us returning with an unfinished
    # iterator.
    for data, metadata in client.get_prefix(queue_path, sort_order='ascend',
                                            sort_target='key', limit=1):
        jobname = str(metadata['key']).split('/')[-1].rstrip("'")

        # Ensure that this task isn't in the future
        if float(jobname.split('-')[0]) > time.time():
            return None, None

        workitem = json.loads(data, object_hook=decodeTasks)
        put('processing', queuename, jobname, workitem)
        client.delete(metadata['key'])
        LOG.with_fields({'jobname': jobname,
                            'queuename': queuename,
                            'workitem': workitem,
                            }).info('Moved workitem from queue to processing')

        return jobname, workitem

    return None, None


def resolve(queuename, jobname):
    if read_only_cache():
        raise exceptions.ForbiddenWhileUsingReadOnlyCache(
            'You cannot resolve queue work items while using a read only cache')

    delete('processing', queuename, jobname)
    LOG.with_fields({'jobname': jobname,
                        'queuename': queuename,
                        }).info('Resolved workitem')


def get_queue_length(queuename):
    queued = 0
    deferred = 0
    for name, _ in get_all('queue', queuename):
        if float(name.split('/')[-1].split('-')[0]) > time.time():
            deferred += 1
        else:
            queued += 1

    processing = len(list(get_all('processing', queuename)))
    return processing, queued, deferred


@retry_etcd_forever
def _restart_queue(queuename):
    queue_path = _construct_key('processing', queuename, None)

    # FIXME(mikal): excluded from using the thread local etcd client because
    # the iterator call interleaves with other etcd requests and causes the wrong
    # data to be handed to the wrong caller.
    for data, metadata in WrappedEtcdClient().get_prefix(
            queue_path, sort_order='ascend'):
        jobname = str(metadata['key']).split('/')[-1].rstrip("'")
        workitem = json.loads(data)
        put('queue', queuename, jobname, workitem)
        delete('processing', queuename, jobname)
        LOG.with_fields({'jobname': jobname,
                            'queuename': queuename,
                            }).warning('Reset workitem')


def get_outstanding_jobs():
    # FIXME(mikal): excluded from using the thread local etcd client because
    # the yield call interleaves with other etcd requests and causes the wrong
    # data to be handed to the wrong caller.
    for data, metadata in WrappedEtcdClient().get_prefix(
            '/sf/processing'):
        yield metadata['key'].decode('utf-8'), json.loads(data, object_hook=decodeTasks)
    for data, metadata in WrappedEtcdClient().get_prefix(
            '/sf/queued'):
        yield metadata['key'].decode('utf-8'), json.loads(data, object_hook=decodeTasks)


def get_current_blob_transfers(absent_nodes=[]):
    current_fetches = defaultdict(list)
    for workname, workitem in get_outstanding_jobs():
        # A workname looks like: /sf/queue/sf-3/jobname
        _, _, phase, node, _ = workname.split('/')
        if node == 'networknode':
            continue

        for task in workitem:
            if isinstance(task, FetchBlobTask):
                if node in absent_nodes:
                    LOG.with_fields({
                        'blob': task.blob_uuid,
                        'node': node,
                        'phase': phase
                    }).warning('Node is absent, ignoring fetch')
                else:
                    LOG.with_fields({
                        'blob': task.blob_uuid,
                        'node': node,
                        'phase': phase
                    }).info('Node is fetching blob')
                    current_fetches[task.blob_uuid].append(node)

    return current_fetches


def restart_queues():
    # Move things which were in processing back to the queue because
    # we didn't complete them before crashing.
    if config.NODE_IS_NETWORK_NODE:
        _restart_queue('networknode')
    _restart_queue(config.NODE_NAME)
