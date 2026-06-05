#!/usr/bin/env python3
"""一次性脚本：将服务器本地已有视频批量迁移到阿里云 OSS"""
import os
import sys
import sqlite3
from datetime import datetime

# 把当前目录加入 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import oss2
except ImportError:
    print("[ERROR] oss2 not installed. Run: pip install oss2")
    sys.exit(1)

# ============ 配置 ============
OSS_ACCESS_KEY = os.environ.get('OSS_ACCESS_KEY', '')
OSS_SECRET_KEY = os.environ.get('OSS_SECRET_KEY', '')
OSS_BUCKET_NAME = 'rkfbalisong'
OSS_ENDPOINT = 'oss-cn-beijing.aliyuncs.com'
OSS_CDN_BASE = 'https://%s.%s' % (OSS_BUCKET_NAME, OSS_ENDPOINT)
OSS_VIDEO_PREFIX = 'videos/'

DB_PATH = '/www/wwwroot/rkf-auth/data/users.db'
UPLOAD_DIR = '/www/wwwroot/rkf-auth/data/uploads/videos/'

# ============ 初始化 OSS ============
auth = oss2.Auth(OSS_ACCESS_KEY, OSS_SECRET_KEY)
bucket = oss2.Bucket(auth, OSS_ENDPOINT, OSS_BUCKET_NAME)
print("[OSS] Connected to bucket: %s" % OSS_BUCKET_NAME)

# ============ 连接数据库 ============
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
db = conn.cursor()

# ============ 查找所有没有 oss_url 的视频 ============
rows = db.execute(
    "SELECT id, filename FROM videos WHERE (oss_url = '' OR oss_url IS NULL) AND filename != ''"
).fetchall()

print("[DB] Found %d videos needing migration" % len(rows))

if len(rows) == 0:
    print("[DONE] No videos to migrate.")
    conn.close()
    sys.exit(0)

success_count = 0
skip_count = 0
fail_count = 0

for row in rows:
    vid = row['id']
    filename = row['filename']
    local_path = os.path.join(UPLOAD_DIR, filename)
    oss_key = OSS_VIDEO_PREFIX + filename
    oss_url = '%s/%s' % (OSS_CDN_BASE, oss_key)

    print("\n--- Video #%d: %s ---" % (vid, filename))

    # 检查本地文件是否存在
    if not os.path.exists(local_path):
        print("  [SKIP] Local file not found: %s" % local_path)
        skip_count += 1
        continue

    file_size = os.path.getsize(local_path)
    file_size_mb = file_size / (1024 * 1024)
    print("  Size: %.1f MB" % file_size_mb)

    # 上传到 OSS
    try:
        print("  Uploading to OSS...")
        bucket.put_object_from_file(oss_key, local_path)
        print("  [OK] Uploaded to: %s" % oss_url)

        # 更新数据库
        db.execute(
            "UPDATE videos SET oss_url = ?, updated_at = ? WHERE id = ?",
            (oss_url, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), vid)
        )
        conn.commit()
        print("  [OK] Database updated: oss_url = %s" % oss_url)

        # 可选：上传成功后删除本地文件以释放空间
        # os.remove(local_path)
        # print("  [OK] Local file deleted to free space")

        success_count += 1

    except Exception as e:
        print("  [FAIL] %s" % e)
        fail_count += 1

conn.close()

print("\n" + "=" * 50)
print("MIGRATION COMPLETE")
print("  Success: %d" % success_count)
print("  Skipped: %d" % skip_count)
print("  Failed:  %d" % fail_count)
print("=" * 50)
