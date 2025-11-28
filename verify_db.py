import os
from bot import Database

DB_FILE = "test_bot_database.db"

def test_db():
    if os.path.exists(DB_FILE):
        os.remove(DB_FILE)
    
    print("Initializing Database...")
    db = Database(DB_FILE)
    
    print("Testing Add Subscription...")
    db.add_subscription(12345, "Python Dev", "Remote")
    
    subs = db.get_all_subscriptions()
    assert len(subs) == 1
    assert subs[0][0] == 12345
    assert subs[0][1] == "Python Dev"
    print("Subscription added successfully.")
    
    print("Testing Job Processing...")
    job_hash = "abc123hash"
    assert not db.is_job_processed(job_hash)
    
    db.mark_job_processed(job_hash)
    assert db.is_job_processed(job_hash)
    print("Job processing logic works.")
    
    print("Testing Remove Subscription...")
    db.remove_subscription(12345)
    subs = db.get_all_subscriptions()
    assert len(subs) == 0
    print("Subscription removed successfully.")
    
    print("ALL DB TESTS PASSED")
    os.remove(DB_FILE)

if __name__ == "__main__":
    test_db()
