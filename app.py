"""
刃客坊 (rkfbalisong.cn) 认证服务 + 视频系统
=====================================
部署在阿里云服务器上，替代 Supabase Edge Functions

功能:
  POST /api/send-sms       - 发送短信验证码（互亿无线）
  POST /api/verify          - 验证码登录/自动注册
  POST /api/login-password  - 密码登录
  POST /api/set-password    - 设置/修改密码
  GET  /api/me              - 验证token获取用户信息
  POST /api/logout          - 注销token
  GET  /api/health          - 健康检查

  POST /api/videos/prepare-upload   - 浏览器直传 OSS 第一步：获取预签名 URL
  POST /api/videos/confirm-upload   - 浏览器直传 OSS 第二步：确认上传完成
  POST /api/videos/upload           - 旧版上传接口（保留兼容）
  GET  /api/videos          - 视频列表（公开，已审核通过）
  GET  /api/videos/admin    - 管理端视频列表（需管理员）
  GET  /api/videos/<id>     - 视频详情
  PUT  /api/videos/<id>/review - 审核视频（管理员）
  DELETE /api/videos/<id>   - 删除视频（管理员）
  GET  /api/videos/file/<name> - 获取视频文件

用户数据: SQLite (data/users.db)
视频文件: 阿里云 OSS (rkfbalisong.oss-cn-beijing.aliyuncs.com/videos/) + CDN加速
照片文件: data/uploads/photos/
验证码: 内存存储

启动: python app.py
或生产环境: gunicorn -w 2 -b 0.0.0.0:5000 app:app

兼容: Python 3.6+
"""

import os
import re
import json
import time
import hashlib
import sqlite3
import logging
import secrets
import random
from datetime import datetime, timedelta

# Load .env file before reading any config
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    load_dotenv(_env_path)
except ImportError:
    pass

from flask import Flask, request, jsonify, g, send_from_directory, send_file
from flask_cors import CORS
from werkzeug.utils import secure_filename
import requests

try:
    import oss2
    OSS_AVAILABLE = True
except ImportError:
    oss2 = None
    OSS_AVAILABLE = False


# ============ 配置 ============
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
os.makedirs(DATA_DIR, exist_ok=True)

# 视频文件上传目录
UPLOAD_DIR = os.path.join(DATA_DIR, 'uploads', 'videos')
os.makedirs(UPLOAD_DIR, exist_ok=True)
MAX_VIDEO_SIZE = 1024 * 1024 * 1024  # 1GB
ALLOWED_VIDEO_EXTENSIONS = {'mp4', 'webm', 'mov', 'avi'}

# 照片文件上传目录
PHOTO_DIR = os.path.join(DATA_DIR, 'uploads', 'photos')
os.makedirs(PHOTO_DIR, exist_ok=True)
MAX_PHOTO_SIZE = 50 * 1024 * 1024  # 50MB per photo
ALLOWED_PHOTO_EXTENSIONS = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'svg', 'heic', 'heif'}

DB_PATH = os.path.join(DATA_DIR, 'users.db')

# ============ 阿里云 OSS 配置 ============
OSS_ACCESS_KEY = os.environ.get('OSS_ACCESS_KEY', '')
OSS_SECRET_KEY = os.environ.get('OSS_SECRET_KEY', '')
OSS_BUCKET_NAME = os.environ.get('OSS_BUCKET', 'rkfbalisong')
OSS_ENDPOINT = os.environ.get('OSS_ENDPOINT', 'oss-cn-beijing.aliyuncs.com')
OSS_CDN_BASE = 'https://%s.%s' % (OSS_BUCKET_NAME, OSS_ENDPOINT)
OSS_VIDEO_PREFIX = 'videos/'  # OSS 中视频文件的前缀路径

# 从环境变量读取配置（兜底使用硬编码值，确保不依赖 .env 也能工作）
IHUDI_ACCOUNT = os.environ.get('IHUDI_VOICE_ACCOUNT', 'C04504191')
IHUDI_APIKEY = os.environ.get('IHUDI_VOICE_APIKEY', '4cd42af58527471a2aa5cfa3a8f0c059')

# Token密钥
AUTH_SECRET = os.environ.get('AUTH_JWT_SECRET', 'rkf-auth-secret-change-me-' + secrets.token_hex(16))
TOKEN_EXPIRY_HOURS = int(os.environ.get('AUTH_TOKEN_EXPIRY', '168'))  # 默认7天

# 日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
log = logging.getLogger('rkf_auth')

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})


# ============ 数据库初始化 ============
def get_db():
    """每个请求获取数据库连接"""
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db_obj = g.pop('db', None)
    if db_obj is not None:
        db_obj.close()


def init_db():
    """初始化数据库表"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT UNIQUE NOT NULL,
        password_hash TEXT DEFAULT '',
        nickname TEXT DEFAULT '',
        is_admin INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')),
        last_login_at TEXT DEFAULT ''
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        expires_at TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS sms_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT NOT NULL,
        code TEXT NOT NULL,
        sent_at TEXT DEFAULT (datetime('now')),
        success INTEGER DEFAULT 0,
        ip TEXT DEFAULT ''
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS verification_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT NOT NULL,
        code TEXT NOT NULL,
        created_at REAL NOT NULL,
        used INTEGER DEFAULT 0
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS favorites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        video_id INTEGER NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(video_id) REFERENCES videos(id),
        UNIQUE(user_id, video_id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS museum_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        brand TEXT DEFAULT '',
        type TEXT DEFAULT '',
        year TEXT DEFAULT '',
        rarity INTEGER DEFAULT 0,
        desc_text TEXT DEFAULT '',
        image_seed TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now'))
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS videos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT DEFAULT '',
        category TEXT DEFAULT 'other',
        tags TEXT DEFAULT '[]',
        filename TEXT NOT NULL,
        oss_url TEXT DEFAULT '',
        author_id INTEGER DEFAULT 0,
        author_name TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',
        reject_reason TEXT DEFAULT '',
        views INTEGER DEFAULT 0,
        likes INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY(author_id) REFERENCES users(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS photos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL DEFAULT '',
        description TEXT DEFAULT '',
        brand TEXT DEFAULT '',
        year TEXT DEFAULT '',
        type TEXT DEFAULT 'collection',
        filename TEXT NOT NULL,
        author_id INTEGER DEFAULT 0,
        author_name TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',
        reject_reason TEXT DEFAULT '',
        views INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY(author_id) REFERENCES users(id)
    )''')

    conn.commit()
    conn.close()
    log.info("Database initialized at %s", DB_PATH)

    # 数据库迁移：为旧数据库添加 oss_url 列（如果不存在）
    try:
        mig_conn = sqlite3.connect(DB_PATH)
        mig_conn.execute("ALTER TABLE videos ADD COLUMN oss_url TEXT DEFAULT ''")
        mig_conn.commit()
        mig_conn.close()
        log.info("[DB Migration] Added oss_url column to videos table")
    except sqlite3.OperationalError:
        pass  # 列已存在，忽略

    # 确保管理员账号存在（重新打开连接）
    try:
        admin_conn = sqlite3.connect(DB_PATH)
        admin_conn.row_factory = sqlite3.Row
        _ensure_admin(admin_conn)
        admin_conn.close()
    except Exception as e:
        log.warning("[Admin] Error ensuring admin: %s", e)


def seed_museum_data():
    """已禁用：不再自动填充默认博物馆数据。博物馆数据现在由用户上传的照片系统管理。"""
    # 博物馆数据现在通过 /api/photos 管理（用户上传+审核）
    # 旧 museum_items 表仅保留已有数据，不再自动填充
    log.info("[Museum] Auto-seeding disabled. Museum uses photo upload system.")


# 模块加载时自动初始化数据库（Gunicorn 生产模式也会执行）
init_db()
seed_museum_data()


# 在每次请求后自动同步管理员（兜底方案）
@app.after_request
def auto_promote_admin(response):
    """每个请求结束后自动检查并提升管理员"""
    try:
        db = get_db()
        for ph in ['+8613611036506', '13611036506', '48613611036506']:
            db.execute("UPDATE users SET is_admin = 1 WHERE phone = ? AND is_admin = 0", (ph,))
        db.commit()
    except Exception:
        pass
    return response


def _ensure_admin(conn):
    """确保管理员手机号被标记为admin"""
    ADMIN_PHONES = ['+8613611036506', '13611036506', '48613611036506']
    c = conn.cursor()
    for ph in ADMIN_PHONES:
        row = c.execute("SELECT id, is_admin FROM users WHERE phone = ?", (ph,)).fetchone()
        if row:
            if not row['is_admin']:
                c.execute("UPDATE users SET is_admin = 1 WHERE phone = ?", (ph,))
                log.info("[Admin] Set admin flag for %s", ph)
        else:
            c.execute(
                "INSERT INTO users (phone, password_hash, is_admin, created_at, last_login_at) "
                "VALUES (?, '', 1, datetime('now'), '')",
                (ph,)
            )
            log.info("[Admin] Created admin account: %s", ph)
    conn.commit()


# ============ OSS 工具函数 ============

def get_oss_bucket():
    """获取 OSS Bucket 对象"""
    if not OSS_AVAILABLE:
        return None
    auth = oss2.Auth(OSS_ACCESS_KEY, OSS_SECRET_KEY)
    return oss2.Bucket(auth, OSS_ENDPOINT, OSS_BUCKET_NAME)


def get_oss_upload_url(oss_key, expires=3600, content_type=None):
    """生成 OSS 直传的预签名 PUT URL（浏览器直接上传用）

    注意：如果前端上传时设置了 Content-Type header，
    生成签名时也必须传入相同的 content_type，否则 OSS 会报签名不匹配。
    """
    bucket = get_oss_bucket()
    if not bucket:
        log.error("[OSS] oss2 not installed, cannot sign url")
        return None
    try:
        headers = None
        if content_type:
            headers = {'Content-Type': content_type}
        return bucket.sign_url('PUT', oss_key, expires, headers=headers)
    except Exception as e:
        log.error("[OSS] sign_url failed: %s", e)
        return None


def upload_to_oss(local_path, oss_key):
    """上传文件到 OSS，返回 CDN URL"""
    bucket = get_oss_bucket()
    if not bucket:
        log.error("[OSS] oss2 not installed, falling back to local")
        return None
    bucket.put_object_from_file(oss_key, local_path)
    oss_url = '%s/%s' % (OSS_CDN_BASE, oss_key)
    log.info("[OSS] Uploaded: %s -> %s", oss_key, oss_url)
    return oss_url


def delete_from_oss(oss_key):
    """从 OSS 删除文件"""
    bucket = get_oss_bucket()
    if not bucket:
        return
    try:
        bucket.delete_object(oss_key)
        log.info("[OSS] Deleted: %s", oss_key)
    except Exception as e:
        log.warning("[OSS] Delete failed for %s: %s", oss_key, e)


def get_oss_url(filename):
    """生成视频的 OSS CDN 地址"""
    return '%s/%s%s' % (OSS_CDN_BASE, OSS_VIDEO_PREFIX, filename)


# ============ OSS 数据库持久化（Railway 等无状态环境使用） ============

OSS_DB_BACKUP_KEY = 'database/users.db'

def restore_db_from_oss():
    """从 OSS 下载数据库备份"""
    bucket = get_oss_bucket()
    if not bucket:
        log.warning("[DB] OSS not available, skip DB restore")
        return False
    try:
        if bucket.object_exists(OSS_DB_BACKUP_KEY):
            bucket.get_object_to_file(OSS_DB_BACKUP_KEY, DB_PATH)
            log.info("[DB] Restored from OSS backup: %s", OSS_DB_BACKUP_KEY)
            return True
        else:
            log.info("[DB] No OSS backup found, starting fresh")
            return False
    except Exception as e:
        log.warning("[DB] OSS restore failed: %s", e)
        return False


def backup_db_to_oss():
    """上传数据库备份到 OSS"""
    bucket = get_oss_bucket()
    if not bucket or not os.path.exists(DB_PATH):
        return False
    try:
        bucket.put_object_from_file(OSS_DB_BACKUP_KEY, DB_PATH)
        log.info("[DB] Backed up to OSS: %s", OSS_DB_BACKUP_KEY)
        return True
    except Exception as e:
        log.warning("[DB] OSS backup failed: %s", e)
        return False


# ============ 验证码管理（数据库） ============
CODE_EXPIRE_SECONDS = 300   # 5分钟有效
CODE_COOLDOWN_SECONDS = 60  # 60秒冷却


def generate_code():
    """生成6位随机验证码"""
    return ''.join([str(random.randint(0, 9)) for _ in range(6)])


def save_code(phone, code):
    """保存验证码到数据库"""
    db = get_db()
    db.execute(
        "INSERT INTO verification_codes (phone, code, created_at, used) VALUES (?, ?, ?, 0)",
        (phone, code, time.time())
    )
    db.commit()
    log.info("[SMS] Code saved for %s: code=%s phone=%s", phone[-4:], code, phone)


def verify_code(phone, input_code):
    """从数据库校验验证码"""
    db = get_db()
    row = db.execute(
        "SELECT id, code, created_at, used FROM verification_codes WHERE phone = ? ORDER BY created_at DESC LIMIT 1",
        (phone,)
    ).fetchone()

    if not row:
        log.warning("[Verify] FAIL: No code found for %s", phone)
        return False

    if row['used']:
        log.warning("[Verify] FAIL: Code already used for %s (id=%d)", phone, row['id'])
        return False

    elapsed = time.time() - row['created_at']
    if elapsed > CODE_EXPIRE_SECONDS:
        log.warning("[Verify] FAIL: Code expired for %s (%.1fs > %ds)", phone, elapsed, CODE_EXPIRE_SECONDS)
        return False

    stored_code = row['code']
    if stored_code != input_code:
        log.warning("[Verify] FAIL: Wrong code for %s (stored=%s input=%s)", phone, stored_code, input_code)
        return False

    db.execute("UPDATE verification_codes SET used = 1 WHERE id = ?", (row['id'],))
    db.commit()
    log.info("[Verify] OK: Code verified for %s (id=%d)", phone, row['id'])
    return True


def check_cooldown(phone):
    """
    从数据库检查冷却状态
    返回: (can_send: bool, remaining_seconds: int)
    """
    db = get_db()
    row = db.execute(
        "SELECT created_at, used FROM verification_codes WHERE phone = ? ORDER BY created_at DESC LIMIT 1",
        (phone,)
    ).fetchone()

    if not row:
        return True, 0

    if row['used']:
        elapsed = time.time() - row['created_at']
        remaining = CODE_COOLDOWN_SECONDS - elapsed
        if remaining > 0:
            return False, int(remaining)
        return True, 0

    elapsed = time.time() - row['created_at']
    if elapsed > CODE_EXPIRE_SECONDS:
        return True, 0

    remaining = CODE_COOLDOWN_SECONDS - elapsed
    if remaining > 0:
        return False, int(remaining)

    return True, 0


# ============ Token 管理 ============
def create_token(user_id):
    """创建登录token"""
    token = 'tk_' + secrets.token_urlsafe(32)
    expires = (datetime.utcnow() + timedelta(hours=TOKEN_EXPIRY_HOURS)).strftime('%Y-%m-%dT%H:%M:%SZ')

    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
        (token, user_id, expires)
    )
    db.commit()

    log.info("[Auth] Token created for user_id=%d", user_id)
    return token


def validate_token(token):
    """验证token，返回用户字典或None"""
    if not token or not token.startswith('tk_'):
        return None

    db = get_db()
    row = db.execute(
        "SELECT s.user_id, u.id, u.phone, u.nickname, u.is_admin, s.expires_at "
        "FROM sessions s JOIN users u ON s.user_id = u.id "
        "WHERE s.token = ?", (token,)
    ).fetchone()

    if not row:
        return None

    try:
        expires_str = row['expires_at']
        now_str = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
        if now_str > expires_str:
            db.execute("DELETE FROM sessions WHERE token = ?", (token,))
            db.commit()
            return None
    except Exception:
        pass

    ADMIN_PHONES = ['+8613611036506', '13611036506', '48613611036506']
    phone = row['phone']
    is_admin = bool(row['is_admin'])
    # 强制检查管理员手机号（兜底）
    if phone in ADMIN_PHONES or phone.replace('+86','') in ADMIN_PHONES:
        is_admin = True
        try:
            db.execute("UPDATE users SET is_admin = 1 WHERE phone = ? AND is_admin = 0", (phone,))
            db.commit()
        except Exception:
            pass

    return {
        'id': row['id'],
        'phone': phone,
        'nickname': row['nickname'] or '',
        'is_admin': is_admin,
    }


def delete_token(token):
    """注销token"""
    db = get_db()
    db.execute("DELETE FROM sessions WHERE token = ?", (token,))
    db.commit()


# ============ 密码工具 ============
def hash_password(password):
    """密码哈希（SHA-256 + salt）"""
    salt = secrets.token_hex(8)
    hashed = hashlib.sha256((password + salt).encode()).hexdigest()
    return salt + '$' + hashed


def verify_password(password, stored):
    """验证密码"""
    if not stored:
        return False
    try:
        idx = stored.index('$')
        salt = stored[:idx]
        hashed = stored[idx+1:]
        new_hash = hashlib.sha256((password + salt).encode()).hexdigest()
        return new_hash == hashed
    except (ValueError, IndexError):
        return hashlib.sha256(password.encode()).hexdigest() == stored


# ============ 互亿无线 短信验证码 ============
def send_voice_sms(phone, code):
    """
    调用互亿无线短信验证码 API
    模板ID=1: 您的验证码是：【变量】。请不要把验证码泄露给其他人。
    返回: {'success': bool, 'msg': str}
    """
    if not IHUDI_ACCOUNT or not IHUDI_APIKEY:
        log.error("[IHuyi] Missing credentials!")
        return {'success': False, 'msg': '短信服务未配置'}

    clean_phone = phone.replace('+86', '').replace('+', '').replace('-', '').strip()
    # 去掉所有前缀，保留纯11位手机号
    if clean_phone.startswith('0'):
        clean_phone = clean_phone[1:]
    if len(clean_phone) > 11:
        clean_phone = clean_phone[-11:]

    try:
        # 互亿自动添加签名，content里不能有【】符号，只需匹配模板格式
        content = '您的验证码是：%s。请不要把验证码泄露给其他人。' % code

        payload = {
            'account': IHUDI_ACCOUNT,
            'password': IHUDI_APIKEY,
            'mobile': clean_phone,
            'content': content,
        }

        log.info("[IHuyi] Sending SMS to %s..., template_content=%s",
                 clean_phone[-4:], content)
        resp = requests.post(
            'https://api.ihuyi.com/sms/Submit.json',
            data=payload,
            timeout=15,
        )
        log.info("[IHuyi] Response status=%d body=%s",
                 resp.status_code, resp.text[:500])
        result = resp.json()

        code_num = result.get('code', -1)
        msg = result.get('msg', '未知错误')

        if code_num == 2:
            log.info("[IHuyi] SMS sent OK! smsid=%s", result.get('smsid'))
            return {'success': True, 'msg': '发送成功'}
        else:
            log.error("[IHuyi] SMS failed: code=%s msg=%s", code_num, msg)
            error_map = {
                '405': '用户名或密码不正确',
                '4050': '账号被冻结',
                '4051': '剩余条数不足',
                '40505': '没有签定合同',
                '4052': '访问IP与备案IP不符',
                '4030': '该号码已被列入黑名单',
                '4072': '模板内容不匹配或签名未审核',
                '4073': '签名内容不匹配',
                '4074': '该时间不允许发送',
                '4075': '手机号数量超限',
            }
            friendly_msg = error_map.get(str(code_num),
                         "服务商返回(%s): %s" % (code_num, msg))
            return {'success': False, 'msg': friendly_msg}

    except requests.exceptions.Timeout:
        log.error("[IHuyi] Request timeout")
        return {'success': False, 'msg': '短信服务响应超时，请稍后重试'}
    except Exception as e:
        log.error("[IHuyi] Error: %s", e)
        return {'success': False, 'msg': '短信服务异常: ' + str(e)}


# ============ API 路由 ============

@app.route('/api/send-sms', methods=['POST'])
def api_send_sms():
    """发送短信验证码"""
    try:
        data = request.get_json(silent=True) or {}
        phone_raw = (data.get('phone') or '').strip()

        phone = re.sub(r'^\+?86?', '', phone_raw)
        phone = '+86' + phone

        if not re.match(r'^\+?861[3-9]\d{9}$', phone):
            return jsonify({
                'success': False,
                'error': '请输入有效的中国手机号'
            }), 400

        # 检查是否已注册（防止重复注册）
        mode = (data.get('mode') or '').strip()
        db = get_db()
        existing = db.execute("SELECT id FROM users WHERE phone = ?", (phone,)).fetchone()

        can_send, remaining = check_cooldown(phone)
        if not can_send:
            return jsonify({
                'success': False,
                'error': '操作太频繁，请%d秒后重试' % remaining
            }), 429

        # 如果是注册模式且账号已存在，拒绝发送验证码
        if mode == 'register' and existing:
            return jsonify({
                'success': False,
                'error': '该账号已存在，请在登录页登录'
            }), 409

        code = generate_code()
        save_code(phone, code)

        sms_result = send_voice_sms(phone, code)

        if sms_result['success']:
            db = get_db()
            db.execute(
                "INSERT INTO sms_logs (phone, code, success, ip) VALUES (?, ?, 1, ?)",
                (phone, code[:2]+'****', request.remote_addr or '')
            )
            db.commit()

            return jsonify({
                'success': True,
                'message': '验证码已发送，请注意查收短信'
            })
        else:
            return jsonify({
                'success': False,
                'error': sms_result['msg']
            }), 500

    except Exception as e:
        log.error("[send-sms] Error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': '服务器内部错误'}), 500


@app.route('/api/verify', methods=['POST'])
def api_verify():
    """验证码登录/注册"""
    try:
        data = request.get_json(silent=True) or {}
        phone_raw = (data.get('phone') or '').strip()
        input_code = (data.get('code') or '').strip()

        phone = re.sub(r'^\+?86?', '', phone_raw)
        phone = '+86' + phone

        if not re.match(r'^\+?861[3-9]\d{9}$', phone):
            return jsonify({'success': False, 'error': '手机号无效'}), 400

        if not input_code or len(input_code) < 4:
            return jsonify({'success': False, 'error': '请输入验证码'}), 400

        if not verify_code(phone, input_code):
            return jsonify({'success': False, 'error': '验证码错误或已过期'}), 400

        db = get_db()
        user = db.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()

        now_iso = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        if user:
            user_id = user['id']
            is_admin = bool(user['is_admin'])
            has_password = bool(user['password_hash'] or '')

            db.execute("UPDATE users SET last_login_at = ? WHERE id = ?",
                       (now_iso, user_id))
            db.commit()

            token = create_token(user_id)

            return jsonify({
                'success': True,
                'message': '登录成功',
                'user': {
                    'id': user_id,
                    'phone': phone,
                    'nickname': (user['nickname'] or ''),
                    'is_admin': is_admin,
                    'need_set_password': not has_password,
                },
                'session': {'access_token': token}
            })
        else:
            cursor = db.execute(
                "INSERT INTO users (phone, password_hash, is_admin, created_at, last_login_at) "
                "VALUES (?, ?, 0, ?, ?)",
                (phone, '', now_iso, now_iso)
            )
            user_id = cursor.lastrowid
            db.commit()

            token = create_token(user_id)
            log.info("[Auth] New user registered: %s (id=%d)", phone, user_id)

            return jsonify({
                'success': True,
                'message': '注册成功',
                'user': {
                    'id': user_id,
                    'phone': phone,
                    'nickname': '',
                    'is_admin': False,
                    'need_set_password': True,
                },
                'session': {'access_token': token}
            })

    except Exception as e:
        log.error("[verify] Error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': '服务器内部错误'}), 500


@app.route('/api/login-password', methods=['POST'])
def api_login_password():
    """密码登录"""
    try:
        data = request.get_json(silent=True) or {}
        phone_raw = (data.get('phone') or '').strip()
        password = (data.get('password') or '').strip()

        phone = re.sub(r'^\+?86?', '', phone_raw)
        phone = '+86' + phone

        if not re.match(r'^\+?861[3-9]\d{9}$', phone):
            return jsonify({'success': False, 'error': '手机号无效'}), 400

        if not password:
            return jsonify({'success': False, 'error': '请输入密码'}), 400

        db = get_db()
        user = db.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()

        if not user:
            return jsonify({'success': False, 'error': '该手机号未注册'}), 401

        stored_hash = (user['password_hash'] or '').strip()
        if not stored_hash:
            return jsonify({
                'success': False,
                'error': '该账号尚未设置密码，请使用验证码登录后设置密码'
            }), 401

        if not verify_password(password, stored_hash):
            return jsonify({'success': False, 'error': '密码错误'}), 401

        now_iso = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        db.execute("UPDATE users SET last_login_at = ? WHERE id = ?",
                   (now_iso, user['id']))
        db.commit()

        token = create_token(user['id'])

        log.info("[Auth] Password login success: %s", phone)

        return jsonify({
            'success': True,
            'message': '登录成功',
            'user': {
                'id': user['id'],
                'phone': phone,
                'nickname': (user['nickname'] or ''),
                'is_admin': bool(user['is_admin']),
                'need_set_password': False,
            },
            'session': {'access_token': token}
        })

    except Exception as e:
        log.error("[login-password] Error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': '服务器内部错误'}), 500


@app.route('/api/set-password', methods=['POST'])
def api_set_password():
    """设置/修改密码（必须登录）"""
    try:
        data = request.get_json(silent=True) or {}
        password = (data.get('password') or '').strip()
        token = request.headers.get('Authorization', '').replace('Bearer ', '')

        # 必须通过 token 验证身份，不允许通过 body 中的 user_id 绕过
        if not token:
            return jsonify({'success': False, 'error': '未提供认证信息'}), 401

        session_user = validate_token(token)
        if not session_user:
            return jsonify({'success': False, 'error': '登录已过期，请重新登录'}), 401

        user_id = session_user['id']

        if not password or len(password) < 6:
            return jsonify({'success': False, 'error': '密码至少6个字符'}), 400

        password_hash = hash_password(password)
        db = get_db()

        user = db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            return jsonify({'success': False, 'error': '用户不存在'}), 404

        db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                   (password_hash, user_id))
        db.commit()

        log.info("[Auth] Password set for user_id=%d", user_id)
        return jsonify({'success': True, 'message': '密码设置成功'})

    except Exception as e:
        log.error("[set-password] Error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': '服务器内部错误'}), 500


@app.route('/api/logout', methods=['POST'])
def api_logout():
    """登出"""
    try:
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if token:
            delete_token(token)
        return jsonify({'success': True, 'message': '已登出'})
    except Exception:
        return jsonify({'success': True, 'message': '已登出'})


@app.route('/api/update-nickname', methods=['POST'])
def api_update_nickname():
    """修改昵称"""
    try:
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            return jsonify({'success': False, 'error': '未登录'}), 401
        session_user = validate_token(token)
        if not session_user:
            return jsonify({'success': False, 'error': '登录已过期'}), 401

        data = request.get_json(silent=True) or {}
        nickname = (data.get('nickname') or '').strip()
        if not nickname:
            return jsonify({'success': False, 'error': '昵称不能为空'}), 400
        if len(nickname) > 20:
            return jsonify({'success': False, 'error': '昵称最多20个字符'}), 400

        user_id = session_user['id']
        db = get_db()
        db.execute("UPDATE users SET nickname = ? WHERE id = ?", (nickname, user_id))
        db.commit()

        log.info("[Auth] Nickname updated for user_id=%d: %s", user_id, nickname)
        return jsonify({'success': True, 'message': '昵称已更新'})

    except Exception as e:
        log.error("[update-nickname] Error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': '服务器内部错误'}), 500


@app.route('/api/me', methods=['GET'])
def api_me():
    """获取当前登录用户信息"""
    token = request.headers.get('Authorization', '').replace('Bearer ', '') or \
            request.args.get('token', '')

    if not token:
        return jsonify({'authenticated': False})

    user = validate_token(token)
    if not user:
        return jsonify({'authenticated': False})

    return jsonify({
        'authenticated': True,
        'user': user
    })


@app.route('/api/health', methods=['GET'])
def api_health():
    """健康检查"""
    return jsonify({
        'status': 'ok',
        'service': 'rkf-auth',
        'time': datetime.now().isoformat(),
        'ihuyi_configured': bool(IHUDI_ACCOUNT and IHUDI_APIKEY),
    })


@app.route('/api/stats', methods=['GET'])
def api_stats():
    """获取站点统计数据"""
    try:
        db = get_db()
        user_count = db.execute("SELECT COUNT(*) as c FROM users").fetchone()['c']
        # 只统计已审核通过的视频
        approved_count = db.execute("SELECT COUNT(*) as c FROM videos WHERE status='approved'").fetchone()['c']
        pending_count = db.execute("SELECT COUNT(*) as c FROM videos WHERE status='pending'").fetchone()['c']
        # 花式视频数量：仅统计已审核通过且分类为 freestyle 的视频
        tricks_count = db.execute("SELECT COUNT(*) as c FROM videos WHERE status='approved' AND category='freestyle'").fetchone()['c']
        # 总视频数（含待审）
        all_videos = db.execute("SELECT COUNT(*) as c FROM videos").fetchone()['c']

        # 博物馆藏品数
        try:
            museum_count = db.execute("SELECT COUNT(*) as c FROM photos WHERE status='approved'").fetchone()['c']
        except Exception:
            museum_count = 0

        return jsonify({
            'success': True,
            'stats': {
                'knives': museum_count,      # 馆藏名刀 = 已审核图片数
                'videos': tricks_count,       # 花式视频 = 已审核花式/自由式视频
                'videos_approved': approved_count,
                'videos_pending': pending_count,
                'tricks': tricks_count,       # 收录招式 = 花式视频数
                'users': user_count,
            }
        })
    except Exception as e:
        log.error("[Stats] Error: %s", e)
        return jsonify({'success': False, 'error': '获取统计数据失败'}), 500


# ============ 调试端点（仅管理员可用） ============
@app.route('/api/debug/codes', methods=['GET'])
def api_debug_codes():
    """调试：查看某手机号的验证码记录（需管理员token）"""
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        return jsonify({'error': '需要管理员权限'}), 401
    session_user = validate_token(token)
    if not session_user or not session_user.get('is_admin'):
        return jsonify({'error': '需要管理员权限'}), 403

    phone_raw = request.args.get('phone', '')
    if not phone_raw:
        return jsonify({'error': 'need phone param'}), 400
    phone = re.sub(r'^\+?86?', '', phone_raw)
    phone = '+86' + phone
    db = get_db()
    rows = db.execute(
        "SELECT id, code, created_at, used, datetime(created_at, 'unixepoch', 'localtime') as time FROM verification_codes WHERE phone = ? ORDER BY created_at DESC LIMIT 5",
        (phone,)
    ).fetchall()
    result = []
    for r in rows:
        result.append(dict(r))
    return jsonify({'phone': phone, 'codes': result})


# ============ 收藏系统 API ============

@app.route('/api/favorites/toggle', methods=['POST'])
def api_favorites_toggle():
    """切换收藏状态（已收藏→取消，未收藏→添加）"""
    try:
        user = _require_auth()
        if not user:
            return jsonify({'success': False, 'error': '请先登录'}), 401

        data = request.get_json(silent=True) or {}
        video_id = data.get('video_id')
        if not video_id:
            return jsonify({'success': False, 'error': '缺少视频ID'}), 400

        db = get_db()
        existing = db.execute(
            "SELECT id FROM favorites WHERE user_id = ? AND video_id = ?",
            (user['id'], video_id)
        ).fetchone()

        if existing:
            db.execute("DELETE FROM favorites WHERE id = ?", (existing['id'],))
            db.commit()
            return jsonify({'success': True, 'favorited': False, 'message': '已取消收藏'})
        else:
            db.execute(
                "INSERT INTO favorites (user_id, video_id) VALUES (?, ?)",
                (user['id'], video_id)
            )
            db.commit()
            return jsonify({'success': True, 'favorited': True, 'message': '已收藏'})

    except Exception as e:
        log.error("[Favorites] toggle error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': '操作失败'}), 500


@app.route('/api/favorites/list', methods=['GET'])
def api_favorites_list():
    """获取当前用户的收藏列表"""
    try:
        user = _require_auth()
        if not user:
            return jsonify({'success': False, 'error': '请先登录'}), 401

        page = max(1, int(request.args.get('page', 1)))
        per_page = min(50, max(1, int(request.args.get('per_page', 20))))

        db = get_db()

        rows = db.execute(
            '''SELECT v.*, f.created_at as favorited_at
               FROM favorites f JOIN videos v ON f.video_id = v.id
               WHERE f.user_id = ?
               ORDER BY f.created_at DESC
               LIMIT ? OFFSET ?''',
            (user['id'], per_page, (page - 1) * per_page)
        ).fetchall()

        total = db.execute(
            "SELECT COUNT(*) as c FROM favorites WHERE user_id = ?",
            (user['id'],)
        ).fetchone()['c']

        base_url = request.host_url.rstrip('/')
        videos = []
        for r in rows:
            v = dict(r)
            v['tags'] = json.loads(v['tags']) if v['tags'] else []
            v['video_url'] = v.get('oss_url') or ('%s/api/videos/file/%s' % (base_url, v['filename']))
            v['duration'] = '--:--'
            v['owner_name'] = v['author_name']
            videos.append(v)

        return jsonify({
            'success': True,
            'videos': videos,
            'total': total,
            'page': page,
            'per_page': per_page,
        })

    except Exception as e:
        log.error("[Favorites] list error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': '获取收藏列表失败'}), 500


@app.route('/api/favorites/check/<int:video_id>', methods=['GET'])
def api_favorites_check(video_id):
    """检查某视频是否已收藏"""
    try:
        user = _require_auth()
        if not user:
            return jsonify({'success': False, 'favorited': False})

        db = get_db()
        row = db.execute(
            "SELECT id FROM favorites WHERE user_id = ? AND video_id = ?",
            (user['id'], video_id)
        ).fetchone()

        return jsonify({'success': True, 'favorited': bool(row)})

    except Exception as e:
        log.error("[Favorites] check error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': '查询失败'}), 500


# ============ 博物馆 API ============

@app.route('/api/museum/items', methods=['GET'])
def api_museum_items():
    """获取博物馆藏品列表"""
    try:
        db = get_db()
        rows = db.execute(
            "SELECT id, name, brand, type, year, rarity, desc_text, image_seed FROM museum_items ORDER BY id ASC"
        ).fetchall()

        items = []
        for r in rows:
            d = dict(r)
            # 构造 tags 对象（与前端原有格式兼容）
            tags = {
                'brand': d['brand'],
                'year': d['year'],
                'rarity': d['rarity'],
                'type': d['type'],
            }
            items.append({
                'id': d['id'],
                'name': d['name'],
                'brand': d['brand'],
                'type': d['type'],
                'year': d['year'],
                'rarity': d['rarity'],
                'desc': d['desc_text'],
                'image_seed': d['image_seed'],
                'tags': tags,
            })

        total = len(items)

        return jsonify({
            'success': True,
            'items': items,
            'total': total,
        })

    except Exception as e:
        log.error("[Museum] list error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': '获取藏品失败'}), 500


@app.route('/api/museum/item/<int:item_id>', methods=['GET'])
def api_museum_item(item_id):
    """获取单个藏品详情"""
    try:
        db = get_db()
        row = db.execute(
            "SELECT id, name, brand, type, year, rarity, desc_text, image_seed FROM museum_items WHERE id = ?",
            (item_id,)
        ).fetchone()

        if not row:
            return jsonify({'success': False, 'error': '藏品不存在'}), 404

        d = dict(row)
        tags = {
            'brand': d['brand'],
            'year': d['year'],
            'rarity': d['rarity'],
            'type': d['type'],
        }

        return jsonify({
            'success': True,
            'item': {
                'id': d['id'],
                'name': d['name'],
                'brand': d['brand'],
                'type': d['type'],
                'year': d['year'],
                'rarity': d['rarity'],
                'desc': d['desc_text'],
                'image_seed': d['image_seed'],
                'tags': tags,
            }
        })

    except Exception as e:
        log.error("[Museum] detail error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': '获取藏品详情失败'}), 500


# ============ 视频系统 API ============

def _require_auth():
    """验证token，返回user dict或None"""
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        return None
    return validate_token(token)


def _require_admin():
    """验证管理员权限，返回user dict或None"""
    user = _require_auth()
    if not user:
        return None
    if not user.get('is_admin'):
        return None
    return user


def _allowed_file(filename):
    """检查文件扩展名是否允许"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_VIDEO_EXTENSIONS


@app.route('/api/videos/upload', methods=['POST'])
def api_video_upload():
    """上传视频（需登录）"""
    try:
        user = _require_auth()
        if not user:
            return jsonify({'success': False, 'error': '请先登录'}), 401

        # 检查文件
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': '未选择文件'}), 400

        f = request.files['file']
        if not f.filename:
            return jsonify({'success': False, 'error': '未选择文件'}), 400

        if not _allowed_file(f.filename):
            return jsonify({'success': False, 'error': '不支持的文件格式，请上传 MP4/WebM/MOV 视频'}), 400

        # 读取元数据
        meta_str = request.form.get('metadata', '{}')
        try:
            metadata = json.loads(meta_str)
        except Exception:
            metadata = {}

        title = (metadata.get('title') or '').strip()
        if not title:
            return jsonify({'success': False, 'error': '请填写视频标题'}), 400

        description = (metadata.get('description') or '').strip()
        category = (metadata.get('category') or 'other').strip()
        tags = json.dumps(metadata.get('tags') or [], ensure_ascii=False)

        # 安全文件名
        orig_name = secure_filename(f.filename)
        ext = orig_name.rsplit('.', 1)[-1].lower() if '.' in orig_name else 'mp4'
        safe_name = '%d_%d.%s' % (user['id'], int(time.time()), ext)
        save_path = os.path.join(UPLOAD_DIR, safe_name)

        # 先保存到本地临时目录
        f.save(save_path)
        file_size = os.path.getsize(save_path)

        if file_size > MAX_VIDEO_SIZE:
            os.remove(save_path)
            return jsonify({'success': False, 'error': '文件超过1GB限制'}), 400

        # 上传到阿里云 OSS
        oss_key = OSS_VIDEO_PREFIX + safe_name
        oss_url = upload_to_oss(save_path, oss_key)

        if oss_url:
            # OSS 上传成功，删除本地文件（节省服务器空间）
            try:
                os.remove(save_path)
                log.info("[Video] Removed local file: %s (uploaded to OSS)", save_path)
            except Exception as e:
                log.warning("[Video] Could not remove local file: %s", e)
        else:
            # OSS 不可用，回退到本地存储
            log.warning("[Video] OSS upload failed, falling back to local storage")
            oss_url = ''

        # 写入数据库
        db = get_db()
        now_iso = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor = db.execute(
            '''INSERT INTO videos
            (title, description, category, tags, filename, oss_url, author_id, author_name,
             status, views, likes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, 0, ?, ?)''',
            (title, description, category, tags, safe_name, oss_url,
             user['id'], user.get('nickname') or user.get('phone') or '匿名用户',
             now_iso, now_iso)
        )
        video_id = cursor.lastrowid
        db.commit()

        log.info("[Video] Uploaded: id=%d user=%s file=%s size=%d oss=%s",
                video_id, user['id'], safe_name, file_size, 'yes' if oss_url else 'no')

        return jsonify({
            'success': True,
            'message': '视频已提交审核',
            'video_id': video_id,
        })

    except Exception as e:
        log.error("[Video Upload] Error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': '上传失败: ' + str(e)}), 500


# ============ 浏览器直传 OSS（新方案，支持大文件） ============

@app.route('/api/videos/prepare-upload', methods=['POST'])
def api_prepare_upload():
    """浏览器直传 OSS 第一步：获取预签名上传 URL"""
    try:
        user = _require_auth()
        if not user:
            return jsonify({'success': False, 'error': '请先登录'}), 401

        data = request.get_json(silent=True) or {}
        title = (data.get('title') or '').strip()
        if not title:
            return jsonify({'success': False, 'error': '请填写视频标题'}), 400

        filename = (data.get('filename') or '').strip()
        if not filename:
            return jsonify({'success': False, 'error': '缺少文件名'}), 400

        file_size = int(data.get('file_size') or 0)
        if file_size > MAX_VIDEO_SIZE:
            return jsonify({'success': False, 'error': '文件超过1GB限制'}), 400

        description = (data.get('description') or '').strip()
        category = (data.get('category') or 'other').strip()
        tags = json.dumps(data.get('tags') or [], ensure_ascii=False)
        content_type = (data.get('content_type') or '').strip()

        # 安全文件名
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'mp4'
        safe_name = '%d_%d.%s' % (user['id'], int(time.time()), ext)
        oss_key = OSS_VIDEO_PREFIX + safe_name

        # 自动配置 OSS CORS（如果未配置，确保浏览器能直传）
        ensure_oss_cors()

        # 生成预签名 URL（1小时有效）
        # 必须传入前端上传时的 Content-Type（xhr.send(file) 会自动设置），否则 OSS 签名校验失败
        if not content_type:
            # 根据扩展名推断 MIME 类型兜底
            ext_map = {'mp4': 'video/mp4', 'webm': 'video/webm', 'mov': 'video/quicktime', 'avi': 'video/x-msvideo'}
            content_type = ext_map.get(ext, 'video/mp4')
        upload_url = get_oss_upload_url(oss_key, expires=3600, content_type=content_type)
        if not upload_url:
            return jsonify({'success': False, 'error': 'OSS 服务暂不可用'}), 503

        # 预创建数据库记录（状态为 pending_upload，等确认后再改为 pending）
        db = get_db()
        now_iso = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor = db.execute(
            '''INSERT INTO videos
            (title, description, category, tags, filename, oss_url, author_id, author_name,
             status, views, likes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending_upload', 0, 0, ?, ?)''',
            (title, description, category, tags, safe_name, '',
             user['id'], user.get('nickname') or user.get('phone') or '匿名用户',
             now_iso, now_iso)
        )
        video_id = cursor.lastrowid
        db.commit()

        return jsonify({
            'success': True,
            'video_id': video_id,
            'upload_url': upload_url,
            'oss_key': oss_key,
            'oss_cdn_url': '%s/%s' % (OSS_CDN_BASE, oss_key),
        })

    except Exception as e:
        log.error("[Prepare Upload] Error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': '服务器内部错误'}), 500


# ============ OSS CORS 自动配置 ============

def ensure_oss_cors():
    """确保 OSS Bucket 配置了正确的 CORS 规则（允许浏览器直传）"""
    bucket = get_oss_bucket()
    if not bucket:
        log.warning("[OSS CORS] oss2 not available, skipping CORS setup")
        return False
    try:
        # 检查是否已有 CORS 规则
        try:
            rules = bucket.get_bucket_cors()
            for rule in rules:
                for origin in rule.allowed_origins:
                    if origin == '*' or 'rkfbalisong' in origin or 'localhost' in origin:
                        log.info("[OSS CORS] Already configured: %s", rule.allowed_origins)
                        return True
        except oss2.exceptions.NoSuchCors:
            pass
        except Exception:
            pass

        # 设置 CORS 规则
        from oss2.models import BucketCorsRule, BucketCors
        rule = BucketCorsRule(
            allowed_origins=['*'],
            allowed_methods=['PUT', 'GET', 'HEAD', 'POST'],
            allowed_headers=['*'],
            expose_headers=['ETag', 'x-oss-request-id'],
            max_age_seconds=3600
        )
        cors = BucketCors([rule])
        bucket.put_bucket_cors(cors)
        log.info("[OSS CORS] Configured successfully: allow all origins, PUT/GET/HEAD/POST")
        return True
    except Exception as e:
        log.warning("[OSS CORS] Failed to configure: %s (OSS key may lack CORS permissions)", e)
        return False


@app.route('/api/videos/confirm-upload', methods=['POST'])
def api_confirm_upload():
    """浏览器直传 OSS 第二步：确认上传完成，更新数据库"""
    try:
        user = _require_auth()
        if not user:
            return jsonify({'success': False, 'error': '请先登录'}), 401

        data = request.get_json(silent=True) or {}
        video_id = int(data.get('video_id') or 0)
        if not video_id:
            return jsonify({'success': False, 'error': '缺少视频ID'}), 400

        db = get_db()
        row = db.execute(
            "SELECT * FROM videos WHERE id = ? AND author_id = ?",
            (video_id, user['id'])
        ).fetchone()

        if not row:
            return jsonify({'success': False, 'error': '视频不存在或无权限'}), 404

        # 更新状态为 pending（等待管理员审核），同时设置 oss_url
        oss_key = OSS_VIDEO_PREFIX + row['filename']
        oss_cdn_url = '%s/%s' % (OSS_CDN_BASE, oss_key)
        now_iso = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        db.execute(
            "UPDATE videos SET status = 'pending', oss_url = ?, updated_at = ? WHERE id = ?",
            (oss_cdn_url, now_iso, video_id)
        )
        db.commit()

        return jsonify({
            'success': True,
            'message': '视频已提交审核',
            'video_id': video_id,
        })

    except Exception as e:
        log.error("[Confirm Upload] Error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': '服务器内部错误'}), 500


@app.route('/api/videos', methods=['GET'])
def api_videos_list():
    """获取已通过审核的视频列表（公开）"""
    try:
        status_filter = request.args.get('status', 'approved')
        category = request.args.get('category', '')
        page = max(1, int(request.args.get('page', 1)))
        per_page = min(50, max(1, int(request.args.get('per_page', 20))))

        db = get_db()

        query = "SELECT * FROM videos WHERE status = ?"
        params = [status_filter]

        if category and category != 'all':
            query += " AND category = ?"
            params.append(category)

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.append(per_page)
        params.append((page - 1) * per_page)

        rows = db.execute(query, params).fetchall()

        # 获取总数
        count_query = "SELECT COUNT(*) as total FROM videos WHERE status = ?"
        count_params = [status_filter]
        if category and category != 'all':
            count_query += " AND category = ?"
            count_params.append(category)
        total = db.execute(count_query, count_params).fetchone()['total']

        base_url = request.host_url.rstrip('/')
        videos = []
        for r in rows:
            v = dict(r)
            v['tags'] = json.loads(v['tags']) if v['tags'] else []
            v['video_url'] = v.get('oss_url') or ('%s/api/videos/file/%s' % (base_url, v['filename']))
            v['duration'] = '--:--'  # 可后续用ffprobe获取
            v['owner_name'] = v['author_name']
            videos.append(v)

        return jsonify({
            'success': True,
            'videos': videos,
            'total': total,
            'page': page,
            'per_page': per_page,
        })

    except Exception as e:
        log.error("[Videos List] Error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': '获取视频列表失败'}), 500


@app.route('/api/videos/admin', methods=['GET'])
def api_videos_admin():
    """管理端：获取视频列表（含所有状态，需管理员）"""
    try:
        admin_user = _require_admin()
        if not admin_user:
            return jsonify({'success': False, 'error': '需要管理员权限'}), 403

        status_filter = request.args.get('status', 'pending')
        db = get_db()

        rows = db.execute(
            "SELECT * FROM videos WHERE status = ? ORDER BY created_at DESC",
            (status_filter,)
        ).fetchall()

        base_url = request.host_url.rstrip('/')
        videos = []
        for r in rows:
            v = dict(r)
            v['tags'] = json.loads(v['tags']) if v['tags'] else []
            v['video_url'] = v.get('oss_url') or ('%s/api/videos/file/%s' % (base_url, v['filename']))

            # 统计各状态数量
        stats = {}
        for s in ['pending', 'approved', 'rejected']:
            c = db.execute("SELECT COUNT(*) as c FROM videos WHERE status = ?", (s,)).fetchone()['c']
            stats[s] = c

        return jsonify({
            'success': True,
            'videos': videos,
            'stats': stats,
        })

    except Exception as e:
        log.error("[Videos Admin] Error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': '获取失败'}), 500


@app.route('/api/videos/<int:vid>', methods=['GET'])
def api_video_detail(vid):
    """获取单个视频详情"""
    try:
        db = get_db()
        row = db.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': '视频不存在'}), 404

        v = dict(row)
        v['tags'] = json.loads(v['tags']) if v['tags'] else []
        base_url = request.host_url.rstrip('/')
        v['video_url'] = v.get('oss_url') or ('%s/api/videos/file/%s' % (base_url, v['filename']))
        v['owner_name'] = v['author_name']

        # 增加播放量
        db.execute("UPDATE videos SET views = views + 1 WHERE id = ?", (vid,))
        db.commit()

        return jsonify({'success': True, 'video': v})

    except Exception as e:
        log.error("[Video Detail] Error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': '获取失败'}), 500


@app.route('/api/videos/<int:vid>/review', methods=['PUT'])
def api_video_review(vid):
    """审核视频（通过/拒绝）- 需管理员"""
    try:
        admin_user = _require_admin()
        if not admin_user:
            return jsonify({'success': False, 'error': '需要管理员权限'}), 403

        data = request.get_json(silent=True) or {}
        action = (data.get('action') or '').strip().lower()
        reason = (data.get('reason') or '').strip()

        if action not in ('approve', 'reject'):
            return jsonify({'success': False, 'error': 'action 必须是 approve 或 reject'}), 400

        db = get_db()
        row = db.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': '视频不存在'}), 404

        new_status = 'approved' if action == 'approve' else 'rejected'
        now_iso = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        if action == 'reject':
            db.execute(
                "UPDATE videos SET status=?, reject_reason=?, updated_at=? WHERE id=?",
                (new_status, reason, now_iso, vid)
            )
            log.info("[Video] Rejected: id=%d by admin=%d reason=%s", vid, admin_user['id'], reason)
        else:
            db.execute(
                "UPDATE videos SET status=?, reject_reason='', updated_at=? WHERE id=?",
                (new_status, now_iso, vid)
            )
            log.info("[Video] Approved: id=%d by admin=%d", vid, admin_user['id'])

        db.commit()
        return jsonify({
            'success': True,
            'message': '已%s' % ('通过审核' if action == 'approve' else '拒绝'),
        })

    except Exception as e:
        log.error("[Video Review] Error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': '操作失败'}), 500


@app.route('/api/videos/<int:vid>', methods=['DELETE'])
def api_video_delete(vid):
    """删除视频 - 管理员或视频发布者可删除"""
    try:
        user = _require_auth()
        if not user:
            return jsonify({'success': False, 'error': '请先登录'}), 401

        db = get_db()
        row = db.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': '视频不存在'}), 404

        # 管理员或视频发布者都可以删除
        is_admin = user.get('is_admin', False)
        if not is_admin and row['author_id'] != user['id']:
            return jsonify({'success': False, 'error': '没有删除权限'}), 403

        # 删除本地文件（如果存在）
        filepath = os.path.join(UPLOAD_DIR, row['filename'])
        if os.path.exists(filepath):
            os.remove(filepath)
            log.info("[Video] Deleted local file: %s", filepath)

        # 删除 OSS 文件（如果存在）
        if row['oss_url']:
            oss_key = OSS_VIDEO_PREFIX + row['filename']
            delete_from_oss(oss_key)

        # 同时删除关联的收藏记录
        db.execute("DELETE FROM favorites WHERE video_id = ?", (vid,))
        db.execute("DELETE FROM videos WHERE id = ?", (vid,))
        db.commit()
        log.info("[Video] Deleted: id=%d by user=%d (admin=%s)", vid, user['id'], is_admin)

        return jsonify({'success': True, 'message': '已删除'})

    except Exception as e:
        log.error("[Video Delete] Error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': '删除失败'}), 500


@app.route('/api/videos/file/<path:filename>', methods=['GET'])
def api_serve_video(filename):
    """提供视频文件下载/播放"""
    # 安全检查：防止路径遍历
    safe = secure_filename(filename)
    if not safe or safe != filename:
        return jsonify({'error': 'Invalid filename'}), 400

    filepath = os.path.join(UPLOAD_DIR, safe)
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found'}), 404

    # 支持Range请求（视频seek）+ 流式传输避免内存溢出
    range_header = request.headers.get('Range', None)
    if range_header:
        try:
            size = os.path.getsize(filepath)
            start = 0
            end = size - 1
            m = re.search(r'bytes=(\d+)-(\d*)', range_header)
            if m:
                start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))
            # 限制每次读取块大小，防止内存溢出（最多8MB每块）
            chunk_size = min(8 * 1024 * 1024, end - start + 1)
            length = end - start + 1

            def generate():
                with open(filepath, 'rb') as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        read_size = min(chunk_size, remaining)
                        data_chunk = f.read(read_size)
                        if not data_chunk:
                            break
                        remaining -= len(data_chunk)
                        yield data_chunk

            resp = app.response_class(
                generate(), status=206,
                mimetype='video/' + (safe.rsplit('.', 1)[-1] or 'mp4'))
            resp.headers.add('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
            resp.headers.add('Accept-Ranges', 'bytes')
            resp.headers.add('Content-Length', str(length))
            resp.headers.add('Cache-Control', 'public, max-age=3600')
            return resp
        except Exception as e:
            log.error("[Serve Video Range] Error: %s", e)
            pass

    return send_file(filepath, mimetype='video/' + (safe.rsplit('.', 1)[-1] or 'mp4'),
                     conditional=True, max_age=3600)


# ============ 照片系统 API ============

def _allowed_photo(filename):
    """检查照片扩展名"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_PHOTO_EXTENSIONS


@app.route('/api/photos/upload', methods=['POST'])
def api_photo_upload():
    """上传照片（需登录）"""
    try:
        user = _require_auth()
        if not user:
            return jsonify({'success': False, 'error': '请先登录'}), 401

        if 'file' not in request.files:
            return jsonify({'success': False, 'error': '未选择文件'}), 400

        f = request.files['file']
        if not f.filename:
            return jsonify({'success': False, 'error': '未选择文件'}), 400

        if not _allowed_photo(f.filename):
            return jsonify({'success': False, 'error': '不支持的图片格式，支持 JPG/PNG/GIF/WebP/BMP/SVG'}), 400

        # 读取元数据
        meta_str = request.form.get('metadata', '{}')
        try:
            metadata = json.loads(meta_str)
        except Exception:
            metadata = {}

        title = (metadata.get('title') or '').strip()
        brand = (metadata.get('brand') or '').strip()
        year = (metadata.get('year') or '').strip()
        ptype = (metadata.get('type') or 'collection').strip()
        description = (metadata.get('description') or '').strip()

        if ptype not in ('trick', 'collection'):
            ptype = 'collection'

        # 安全文件名
        orig_name = secure_filename(f.filename)
        ext = orig_name.rsplit('.', 1)[-1].lower() if '.' in orig_name else 'jpg'
        safe_name = 'photo_%d_%d.%s' % (user['id'], int(time.time()), ext)
        save_path = os.path.join(PHOTO_DIR, safe_name)

        f.save(save_path)
        file_size = os.path.getsize(save_path)

        if file_size > MAX_PHOTO_SIZE:
            os.remove(save_path)
            return jsonify({'success': False, 'error': '图片超过50MB限制'}), 400

        db = get_db()
        now_iso = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor = db.execute(
            '''INSERT INTO photos
            (title, description, brand, year, type, filename, author_id, author_name, status, views, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)''',
            (title, description, brand, year, ptype, safe_name,
             user['id'], user.get('nickname') or user.get('phone') or '匿名用户',
             now_iso, now_iso)
        )
        photo_id = cursor.lastrowid
        db.commit()

        log.info("[Photo] Uploaded: id=%d user=%s file=%s size=%d", photo_id, user['id'], safe_name, file_size)

        return jsonify({'success': True, 'message': '图片已提交审核', 'photo_id': photo_id})

    except Exception as e:
        log.error("[Photo Upload] Error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': '上传失败: ' + str(e)}), 500


@app.route('/api/photos', methods=['GET'])
def api_photos_list():
    """获取已审核通过的图片列表（公开，博物馆用）"""
    try:
        brand = request.args.get('brand', '')
        year = request.args.get('year', '')
        ptype = request.args.get('type', '')
        page = max(1, int(request.args.get('page', 1)))
        per_page = min(50, max(1, int(request.args.get('per_page', 20))))

        db = get_db()
        query = "SELECT * FROM photos WHERE status = 'approved'"
        params = []

        if brand:
            query += " AND brand = ?"
            params.append(brand)
        if year:
            query += " AND year = ?"
            params.append(year)
        if ptype:
            query += " AND type = ?"
            params.append(ptype)

        # 获取总数
        count_query = query.replace("SELECT *", "SELECT COUNT(*) as total")
        total = db.execute(count_query, params).fetchone()['total']

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.append(per_page)
        params.append((page - 1) * per_page)

        rows = db.execute(query, params).fetchall()
        base_url = request.host_url.rstrip('/')

        photos = []
        for r in rows:
            p = dict(r)
            p['photo_url'] = '%s/api/photos/file/%s' % (base_url, p['filename'])
            p['thumbnail_url'] = p['photo_url']
            photos.append(p)

        # 获取所有可选品牌/年代/类型用于筛选
        brands = [r['brand'] for r in db.execute("SELECT DISTINCT brand FROM photos WHERE status='approved' AND brand != '' ORDER BY brand").fetchall()]
        years_list = [r['year'] for r in db.execute("SELECT DISTINCT year FROM photos WHERE status='approved' AND year != '' ORDER BY year DESC").fetchall()]

        return jsonify({
            'success': True,
            'photos': photos,
            'total': total,
            'page': page,
            'per_page': per_page,
            'filters': {'brands': brands, 'years': years_list}
        })

    except Exception as e:
        log.error("[Photos List] Error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': '获取照片列表失败'}), 500


@app.route('/api/photos/admin', methods=['GET'])
def api_photos_admin():
    """管理端：获取照片列表（含所有状态，需管理员）"""
    try:
        admin_user = _require_admin()
        if not admin_user:
            return jsonify({'success': False, 'error': '需要管理员权限'}), 403

        status_filter = request.args.get('status', 'pending')
        db = get_db()

        rows = db.execute(
            "SELECT * FROM photos WHERE status = ? ORDER BY created_at DESC",
            (status_filter,)
        ).fetchall()

        base_url = request.host_url.rstrip('/')
        photos = []
        for r in rows:
            p = dict(r)
            p['photo_url'] = '%s/api/photos/file/%s' % (base_url, p['filename'])
            photos.append(p)

        stats = {}
        for s in ['pending', 'approved', 'rejected']:
            c = db.execute("SELECT COUNT(*) as c FROM photos WHERE status = ?", (s,)).fetchone()['c']
            stats[s] = c

        return jsonify({'success': True, 'photos': photos, 'stats': stats})

    except Exception as e:
        log.error("[Photos Admin] Error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': '获取失败'}), 500


@app.route('/api/photos/<int:pid>', methods=['GET'])
def api_photo_detail(pid):
    """获取单个照片详情"""
    try:
        db = get_db()
        row = db.execute("SELECT * FROM photos WHERE id = ?", (pid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': '照片不存在'}), 404

        p = dict(row)
        base_url = request.host_url.rstrip('/')
        p['photo_url'] = '%s/api/photos/file/%s' % (base_url, p['filename'])

        db.execute("UPDATE photos SET views = views + 1 WHERE id = ?", (pid,))
        db.commit()

        return jsonify({'success': True, 'photo': p})

    except Exception as e:
        log.error("[Photo Detail] Error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': '获取失败'}), 500


@app.route('/api/photos/<int:pid>/review', methods=['PUT'])
def api_photo_review(pid):
    """审核照片 - 需管理员"""
    try:
        admin_user = _require_admin()
        if not admin_user:
            return jsonify({'success': False, 'error': '需要管理员权限'}), 403

        data = request.get_json(silent=True) or {}
        action = (data.get('action') or '').strip().lower()
        reason = (data.get('reason') or '').strip()

        if action not in ('approve', 'reject'):
            return jsonify({'success': False, 'error': 'action 必须是 approve 或 reject'}), 400

        db = get_db()
        row = db.execute("SELECT * FROM photos WHERE id = ?", (pid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': '照片不存在'}), 404

        new_status = 'approved' if action == 'approve' else 'rejected'
        now_iso = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        db.execute(
            "UPDATE photos SET status=?, reject_reason=?, updated_at=? WHERE id=?",
            (new_status, reason if action == 'reject' else '', now_iso, pid)
        )
        db.commit()

        log.info("[Photo] %s: id=%d by admin=%d", 'Approved' if action == 'approve' else 'Rejected', pid, admin_user['id'])
        return jsonify({'success': True, 'message': '已' + ('通过审核' if action == 'approve' else '拒绝')})

    except Exception as e:
        log.error("[Photo Review] Error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': '操作失败'}), 500


@app.route('/api/photos/<int:pid>', methods=['DELETE'])
def api_photo_delete(pid):
    """删除照片 - 管理员或上传者可删除"""
    try:
        user = _require_auth()
        if not user:
            return jsonify({'success': False, 'error': '请先登录'}), 401

        db = get_db()
        row = db.execute("SELECT * FROM photos WHERE id = ?", (pid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': '照片不存在'}), 404

        is_admin = user.get('is_admin', False)
        if not is_admin and row['author_id'] != user['id']:
            return jsonify({'success': False, 'error': '没有删除权限'}), 403

        filepath = os.path.join(PHOTO_DIR, row['filename'])
        if os.path.exists(filepath):
            os.remove(filepath)

        db.execute("DELETE FROM photos WHERE id = ?", (pid,))
        db.commit()
        log.info("[Photo] Deleted: id=%d by user=%d", pid, user['id'])

        return jsonify({'success': True, 'message': '已删除'})

    except Exception as e:
        log.error("[Photo Delete] Error: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': '删除失败'}), 500


@app.route('/api/photos/file/<path:filename>', methods=['GET'])
def api_serve_photo(filename):
    """提供照片文件"""
    safe = secure_filename(filename)
    if not safe or safe != filename:
        return jsonify({'error': 'Invalid filename'}), 400

    filepath = os.path.join(PHOTO_DIR, safe)
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found'}), 404

    ext = safe.rsplit('.', 1)[-1].lower() if '.' in safe else 'jpg'
    mime_map = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
                'gif': 'image/gif', 'webp': 'image/webp', 'bmp': 'image/bmp',
                'svg': 'image/svg+xml'}
    mime = mime_map.get(ext, 'image/jpeg')

    return send_file(filepath, mimetype=mime)


# ============ 错误处理 ============
@app.after_request
def after_request_func(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = \
        'GET, POST, OPTIONS, PUT, DELETE'
    response.headers['Access-Control-Allow-Headers'] = \
        'Content-Type, Authorization, X-Requested-With'
    return response


@app.before_request
def handle_options():
    if request.method == 'OPTIONS':
        resp = app.make_default_options_response()
        return resp


# ============ 启动入口 ============
if __name__ == '__main__':
    print("=" * 50)
    print("  刃客坊认证服务 (RKF Auth Service)")
    print("=" * 50)

    # 从 OSS 恢复数据库（Railway 无状态环境）
    restore_db_from_oss()
    init_db()

    # 自动配置 OSS CORS（让浏览器可以直接上传到 OSS）
    ensure_oss_cors()

    # 启动时备份数据库到 OSS
    backup_db_to_oss()

    # 后台线程：每 60 秒自动备份数据库到 OSS
    import threading
    def auto_backup():
        while True:
            threading.Event().wait(60)
            backup_db_to_oss()
    t = threading.Thread(target=auto_backup, daemon=True)
    t.start()

    port = int(os.environ.get('PORT', 5000))
    host = os.environ.get('HOST', '0.0.0.0')

    print("  服务地址: http://%s:%d" % (host, port))
    print("  API文档:  http://%s:%d/api/health" % (host, port))
    print("  数据库:   %s" % DB_PATH)
    print("  OSS备份:   %s (每60秒自动备份)" % OSS_DB_BACKUP_KEY)
    ihuyi_status = '已配置' if (IHUDI_ACCOUNT and IHUDI_APIKEY) else '未配置!'
    print("  互亿无线: %s (短信)" % ihuyi_status)
    print("=" * 50)
    print()

    app.run(host=host, port=port, debug=False)
