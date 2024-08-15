import redis
import subprocess
import atexit
import os
import logging
from typing import Any
from functools import cached_property, lru_cache
from ..filehashcache import BaseHashCache
import time

logger = logging.getLogger(__name__)

# Global variables
_process_started = False
_container_id = None

def _start_redis_server(host='localhost', port=6379, db=0, data_dir='.cache'):
    global _process_started, _container_id

    if _process_started:
        return

    # Convert data_dir to absolute path
    abs_data_dir = os.path.abspath(data_dir)

    try:
        # First, try to connect to Redis
        redis_client = redis.Redis(host=host, port=port, db=db)
        redis_client.ping()
        _process_started = True
        logger.info("Redis server is already running and accessible")
        return
    except (redis.exceptions.ConnectionError, redis.exceptions.ResponseError):
        logger.info("Unable to connect to Redis. Checking Docker container status.")

    try:
        # Check if a Redis container already exists
        result = subprocess.run(
            ['docker', 'ps', '-a', '--filter', f'name=redis-{port}', '--format', '{{.ID}}'],
            capture_output=True,
            text=True,
            check=True
        )
        existing_container = result.stdout.strip()

        if existing_container:
            logger.info(f"Existing Redis container found: {existing_container}")
            # Check if the container is running
            result = subprocess.run(
                ['docker', 'inspect', '-f', '{{.State.Running}}', existing_container],
                capture_output=True,
                text=True,
                check=True
            )
            is_running = result.stdout.strip() == 'true'

            if is_running:
                logger.info("Existing container is already running. Using it.")
                _container_id = existing_container
            else:
                logger.info("Starting existing container.")
                subprocess.run(['docker', 'start', existing_container], check=True)
                _container_id = existing_container
        else:
            logger.info("No existing Redis container found. Starting a new one.")
            result = subprocess.run(
                [
                    'docker', 'run', '-d',
                    '--name', f'redis-{port}',
                    '-p', f'{port}:{port}',
                    '-v', f'{abs_data_dir}:/data',  # Use absolute path here
                    'redis',
                    'redis-server', '--appendonly', 'yes'
                ],
                capture_output=True,
                text=True,
                check=True
            )
            _container_id = result.stdout.strip()
            logger.info(f"Redis Docker container started with ID: {_container_id}")

        # Wait for Redis to be ready
        max_retries = 30
        for _ in range(max_retries):
            try:
                redis_client = redis.Redis(host=host, port=port, db=db)
                redis_client.ping()
                _process_started = True
                logger.info("Redis server is ready to accept connections")
                return
            except (redis.exceptions.ConnectionError, redis.exceptions.ResponseError):
                time.sleep(1)
        
        raise TimeoutError("Redis server did not start within the expected time")
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to start Redis Docker container: {e.stderr}", exc_info=True)
        raise RuntimeError("Failed to start Redis Docker container")
    except Exception as e:
        logger.error(f"Unexpected error while starting Redis Docker container: {str(e)}", exc_info=True)
        raise

def _stop_redis_server():
    global _process_started, _container_id
    if _process_started and _container_id:
        logger.info(f"Stopping Redis Docker container: {_container_id}")
        subprocess.run(['docker', 'stop', _container_id], check=True)
        _process_started = False
        _container_id = None

# Start Redis server on import
_start_redis_server()

# Register stop function to be called on exit
# atexit.register(_stop_redis_server)

class RedisHashCacheModel(BaseHashCache):
    engine = 'redis'
    filename = 'data'

    def __init__(
        self,
        root_dir: str = ".cache",
        compress: bool = None,
        b64: bool = None,
        host: str = 'localhost',
        port: int = 6379,
        db: int = 0,
    ) -> None:
        super().__init__(
            root_dir=root_dir,
            compress=compress,
            b64=b64,
            ensure_dir=False
        )
        self.host = host
        self.port = port
        self.db = db
        self.data_dir = os.path.abspath(self.path)  # Use absolute path here
        logger.info(f"Initialized RedisHashCache with host={host}, port={port}, db={db}")

    @cached_property
    def client(self):
        logger.info(f"Connecting to Redis at {self.host}:{self.port}")
        return redis.Redis(host=self.host, port=self.port, db=self.db)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if 'client' in self.__dict__:
            logger.info("Closing Redis client connection")
            self.client.close()
            del self.__dict__['client']

    def __setitem__(self, key: str, value: Any) -> None:
        encoded_key = self._encode_key(key)
        encoded_value = self._encode_value(value)
        self.client.set(encoded_key, encoded_value)
        logger.debug(f"Set key: {key}")

    def __getitem__(self, key: str) -> Any:
        encoded_key = self._encode_key(key)
        encoded_value = self.client.get(encoded_key)
        if encoded_value is None:
            logger.warning(f"Key not found: {key}")
            raise KeyError(key)
        logger.debug(f"Retrieved key: {key}")
        return self._decode_value(encoded_value)

    def __contains__(self, key: str) -> bool:
        encoded_key = self._encode_key(key)
        exists = self.client.exists(encoded_key) == 1
        logger.debug(f"Checked existence of key: {key}, result: {exists}")
        return exists

    def clear(self) -> None:
        logger.info("Clearing Redis cache")
        self.client.flushdb()
        # self._stop_process()

    def __len__(self) -> int:
        size = self.client.dbsize()
        logger.debug(f"Cache size: {size}")
        return size

    def __iter__(self):
        logger.debug("Iterating over cache keys")
        for key in self.client.scan_iter():
            yield key.decode()


cache = lru_cache(maxsize=None)

@cache
def RedisHashCache(*args, **kwargs):
    return RedisHashCacheModel(*args,**kwargs)