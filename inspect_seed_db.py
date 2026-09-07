import zipfile, sqlite3, tempfile, os

IPA = r"c:\Users\sunck\home\projects\ios\artifacts\Kline-unsigned-ipa\Kline.ipa"
z = zipfile.ZipFile(IPA)
tmp = os.path.join(tempfile.gettempdir(), "seed_tdx.db")
with open(tmp, "wb") as f:
    f.write(z.read("Payload/Kline.app/tdx.db"))

conn = sqlite3.connect(tmp)
c = conn.cursor()
tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("表:", tables)
for t in tables:
    try:
        n = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t} 行数: {n}")
    except Exception as e:
        print(f"  {t} 错误: {e}")
print("meta 前5:", c.execute("SELECT id, code, name, type FROM meta LIMIT 5").fetchall())
conn.close()
