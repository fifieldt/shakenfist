import hashlib
import os
import random
import re
import requests
import shutil
import uuid

from shakenfist.artifact import Artifact, BLOB_URL
from shakenfist import blob
from shakenfist.blob import Blob
from shakenfist.config import config
from shakenfist import exceptions
from shakenfist import image_resolver
from shakenfist import logutil
from shakenfist import util


LOG, _ = logutil.setup(__name__)


class ImageFetchHelper(object):
    def __init__(self, inst, url):
        self.instance = inst
        self.url = url

        self.__artifact = Artifact.from_url(Artifact.TYPE_IMAGE, self.url)
        self.log = LOG.with_fields(
            {'url': self.url, 'artifact': self.__artifact.uuid})

    def get_image(self):
        with self.__artifact.get_lock() as lock:
            url, checksum, checksum_type = image_resolver.resolve(self.url)

            # If this is a request for a URL, do we have the most recent version
            # somewhere in the cluster?
            if not url.startswith(BLOB_URL):
                most_recent = self.__artifact.most_recent_index
                dirty = False

                if most_recent.get('index', 0) == 0:
                    self.log.info('Cluster does not have a copy of image')
                    dirty = True
                else:
                    most_recent_blob = Blob.from_db(most_recent['blob_uuid'])
                    resp = self._open_connection(url)

                    if most_recent_blob.modified != resp.headers.get('Last-Modified'):
                        self.add_event('image requires fetch', None, None,
                                       'Last-Modified: %s -> %s' % (most_recent_blob.modified,
                                                                    resp.headers.get('Last-Modified')))
                        dirty = True

                    if most_recent_blob.size != resp.headers.get('Content-Length'):
                        self.add_event('image requires fetch', None, None,
                                       'Content-Length: %s -> %s' % (most_recent_blob.size,
                                                                     resp.headers.get('Content-Length')))
                        dirty = True

                    self.log.info('Cluster cached image is stale')

                if not dirty:
                    url = '%s%s' % (BLOB_URL, most_recent_blob.uuid)
                    self.log.info('Using cached image from cluster')

            # Ensure that we have the blob in the local store. This blob is in the
            # "original format" if downloaded from an HTTP source.
            if url.startswith(BLOB_URL):
                self.log.info('Fetching image from within the cluster')
                b = self._blob_get(url)
            else:
                self.log.info('Fetching image from the internet')
                b = self._http_get_inner(lock, url, checksum, checksum_type)

            # If this blob uuid is not the most recent index for the artifact, set that
            if self.__artifact.most_recent_index.get('blob_uuid') != b.uuid:
                self.__artifact.add_index(b.uuid)

            # Transcode if required, placing the transcoded file in a well known location.
            if not os.path.exists(os.path.join(config.STORAGE_PATH, 'image_cache', b.uuid + '.qcow2')):
                blob_path = os.path.join(config.STORAGE_PATH, 'blobs', b.uuid)
                if b.info.get('mime-type', '') == 'application/gzip':
                    cache_path = os.path.join(
                        config.STORAGE_PATH, 'image_cache', b.uuid)
                    with util.RecordedOperation('decompress image', self.instance):
                        util.execute([lock], 'gunzip -k -q -c %s > %s'
                                     % (blob_path, cache_path))
                    blob_path = cache_path

                os.makedirs(
                    os.path.join(config.STORAGE_PATH, 'image_cache'), exist_ok=True)
                cache_path = os.path.join(
                    config.STORAGE_PATH, 'image_cache', b.uuid + '.qcow2')
                if identify(blob_path).get('file format', '') == 'qcow2':
                    try:
                        os.link(blob_path, cache_path)
                        self.log.with_fields({'blob': b}).info(
                            'Hard linking %s -> %s' % (blob_path, cache_path))
                    except OSError:
                        os.symlink(blob_path, cache_path)
                        self.log.with_fields({'blob': b}).info(
                            'Symbolic linking %s -> %s' % (blob_path, cache_path))

                    shutil.chown(cache_path, config.LIBVIRT_USER,
                                 config.LIBVIRT_GROUP)
                else:
                    with util.RecordedOperation('transcode image', self.instance):
                        self.log.with_fields({'blob': b}).info(
                            'Transcoding %s -> %s' % (blob_path, cache_path))
                        create_qcow2([lock], blob_path, cache_path)

                shutil.chown(cache_path, 'libvirt-qemu', 'libvirt-qemu')
                self.log.with_fields(util.stat_log_fields(cache_path)).info(
                    'Cache file %s created' % cache_path)

            self.__artifact.state = Artifact.STATE_CREATED

    def _blob_get(self, url):
        """Fetch a blob from the cluster."""

        blob_uuid = url[len(BLOB_URL):]
        blob_dir = os.path.join(config.STORAGE_PATH, 'blobs')
        blob_path = os.path.join(blob_dir, blob_uuid)
        os.makedirs(blob_dir, exist_ok=True)

        b = Blob.from_db(blob_uuid)
        locations = b.locations
        random.shuffle(locations)
        blob_source = locations[0]

        if not os.path.exists(blob_path):
            with util.RecordedOperation('fetch blob', self.instance):
                url = 'http://%s:%d/blob/%s' % (blob_source, config.API_PORT,
                                                blob_uuid)
                admin_token = util.get_api_token(
                    'http://%s:%d' % (blob_source, config.API_PORT))
                r = requests.request('GET', url,
                                     headers={'Authorization': admin_token,
                                              'User-Agent': util.get_user_agent()})

                with open(blob_path + '.partial', 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)

                os.rename(blob_path + '.partial', blob_path)
                b.observe()

        return b

    def _http_get_inner(self, lock, url, checksum, checksum_type):
        """Fetch image if not downloaded and return image path."""

        with util.RecordedOperation('fetch image', self.instance):
            resp = self._open_connection(url)
            blob_uuid = str(uuid.uuid4())
            self.log.with_fields({
                'artifact': self.__artifact.uuid,
                'blob': blob_uuid,
                'url': url}).info('Commencing HTTP fetch to blob')
            b = blob.http_fetch(resp, blob_uuid, [lock], self.log)

            # Ensure checksum is correct
            if not verify_checksum(
                    os.path.join(config.STORAGE_PATH, 'blobs', b.uuid),
                    checksum, checksum_type):
                self.instance.add_event('fetch image', 'bad checksum')
                raise exceptions.BadCheckSum('url=%s' % url)

            # Only persist values after the file has been verified.
            b.observe()
            return b

    def _open_connection(self, url):
        proxies = {}
        if config.HTTP_PROXY_SERVER:
            proxies['http'] = config.HTTP_PROXY_SERVER

        resp = requests.get(url, allow_redirects=True, stream=True,
                            headers={'User-Agent': util.get_user_agent()},
                            proxies=proxies)
        if resp.status_code != 200:
            raise exceptions.HTTPError(
                'Failed to fetch HEAD of %s (status code %d)'
                % (url, resp.status_code))
        return resp


def verify_checksum(image_name, checksum, checksum_type):
    log = LOG.with_field('image', image_name)

    if not checksum:
        log.info('No checksum comparison available')
        return True

    if not os.path.exists(image_name):
        return False

    if checksum_type == 'md5':
        # MD5 chosen because cirros 90% of the time has MD5SUMS available...
        md5_hash = hashlib.md5()
        with open(image_name, 'rb') as f:
            for byte_block in iter(lambda: f.read(4096), b''):
                md5_hash.update(byte_block)
        calc = md5_hash.hexdigest()
        log.with_field('calc', calc).debug('Calc from image download')

        correct = calc == checksum
        log.with_field('correct', correct).info('Image checksum verification')
        return correct

    else:
        raise exceptions.UnknownChecksumType(checksum_type)


VALUE_WITH_BRACKETS_RE = re.compile(r'.* \(([0-9]+) bytes\)')


def identify(path):
    """Work out what an image is."""

    if not os.path.exists(path):
        return {}

    out, _ = util.execute(None, 'qemu-img info %s' % path)

    data = {}
    for line in out.split('\n'):
        line = line.lstrip().rstrip()
        elems = line.split(': ')
        if len(elems) > 1:
            key = elems[0]
            value = ': '.join(elems[1:])

            m = VALUE_WITH_BRACKETS_RE.match(value)
            if m:
                value = float(m.group(1))

            elif value.endswith('K'):
                value = float(value[:-1]) * 1024
            elif value.endswith('M'):
                value = float(value[:-1]) * 1024 * 1024
            elif value.endswith('G'):
                value = float(value[:-1]) * 1024 * 1024 * 1024
            elif value.endswith('T'):
                value = float(value[:-1]) * 1024 * 1024 * 1024 * 1024

            try:
                data[key] = float(value)
            except Exception:
                data[key] = value

    return data


def create_cow(locks, cache_file, disk_file, disk_size):
    """Create a COW layer on top of the image cache.

    disk_size is specified in Gigabytes.
    """

    if os.path.exists(disk_file):
        return

    if disk_size:
        util.execute(locks,
                     'qemu-img create -b %s -f qcow2 %s %dG'
                     % (cache_file, disk_file, int(disk_size)))
    else:
        util.execute(locks,
                     'qemu-img create -b %s -f qcow2 %s'
                     % (cache_file, disk_file))


def create_qcow2(locks, cache_file, disk_file):
    """Make a qcow2 copy of the disk from the image cache."""

    if os.path.exists(disk_file):
        return

    util.execute(locks,
                 'qemu-img convert -t none -O qcow2 %s %s'
                 % (cache_file, disk_file))


def create_blank(locks, disk_file, disk_size):
    """Make an empty image."""

    if os.path.exists(disk_file):
        return

    util.execute(locks, 'qemu-img create -f qcow2 %s %sG'
                 % (disk_file, disk_size))


def snapshot(locks, source, destination):
    """Convert a possibly COW layered disk file into a snapshot."""

    util.execute(locks,
                 'qemu-img convert --force-share -O qcow2 %s %s'
                 % (source, destination))


def resize(locks, input, output, size):
    if os.path.exists(output):
        return

    current_size = identify(input).get('virtual size')

    if current_size == size * 1024 * 1024 * 1024:
        os.link(input, output)
        return

    create_cow(locks, input, output, size)
