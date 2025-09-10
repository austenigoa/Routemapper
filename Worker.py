import os
import redis
from rq import Worker, Queue

# Define the queues to listen to
listen = ['default']

# Get Redis connection URL from environment
redis_url = os.getenv('REDIS_URL')

# Create Redis connection
conn = redis.from_url(redis_url)

if __name__ == '__main__':
    # Create queues with explicit connection
    queues = [Queue(name, connection=conn) for name in listen]

    # Start the worker with the Redis connection
    worker = Worker(queues, connection=conn)
    worker.work()
