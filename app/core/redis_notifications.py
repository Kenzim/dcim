"""
Redis keyspace notifications handler for cleaning up expired tokens
"""
import json
import asyncio
import logging
from app.core.redis import redis_client

logger = logging.getLogger(__name__)


def handle_expired_token(key: str):
    """
    Handle expired token notification.
    Called when a token key expires in Redis.
    Key format: tok:{token_id}
    """
    try:
        # Key format: tok:{token_id}
        if not key.startswith("tok:"):
            return
        
        token_id = key.replace("tok:", "")
        
        # Need to find which user this token belongs to
        # We can get it from the expired HASH if still available, or scan ZSETs
        # For now, we'll rely on lazy cleanup in sessions endpoint
        # In production, you might want to store token_id->user_id mapping separately
        
        logger.debug(f"Token expired: {token_id[:8]}...")
        
        # Note: ZSET cleanup happens lazily when sessions endpoint is called
        # For immediate cleanup, you could scan user_toks:* ZSETs, but that's expensive
        # Better to do lazy cleanup or maintain a reverse index
        
    except Exception as e:
        logger.error(f"Error handling expired token {key}: {e}")


async def start_keyspace_notification_listener():
    """
    Start listening for Redis keyspace notifications.
    Requires Redis config: notify-keyspace-events Ex
    Runs in background thread to avoid blocking event loop.
    
    Note: With tok:{token_id} format, we can't easily get user_id from expired key.
    ZSET cleanup happens lazily in sessions endpoint.
    """
    import threading
    
    def listen_thread():
        """Run pubsub listener in separate thread"""
        try:
            # Subscribe to expired key events
            pubsub = redis_client.pubsub()
            pubsub.psubscribe("__keyevent@0__:expired")
            
            logger.info("Started Redis keyspace notification listener")
            
            for message in pubsub.listen():
                if message["type"] == "pmessage":
                    key = message["data"].decode("utf-8")
                    handle_expired_token(key)
        except Exception as e:
            logger.error(f"Error in keyspace notification listener: {e}")
    
    # Start listener in background thread
    thread = threading.Thread(target=listen_thread, daemon=True)
    thread.start()
    logger.info("Redis keyspace notification listener thread started")


def setup_keyspace_notifications():
    """
    Configure Redis to send keyspace notifications.
    Note: This requires Redis server configuration.
    Call redis-cli: CONFIG SET notify-keyspace-events Ex
    """
    try:
        # Try to enable keyspace notifications (may require Redis admin access)
        redis_client.config_set("notify-keyspace-events", "Ex")
        logger.info("Enabled Redis keyspace notifications")
    except Exception as e:
        logger.warning(f"Could not enable keyspace notifications (may require admin): {e}")
        logger.warning("To enable manually, run: redis-cli CONFIG SET notify-keyspace-events Ex")

