# -*- coding: utf-8 -*-
"""Excel 导入模板 / 逐行校验 / 导出 通用工具（DATA-001/002、ERP-108、ERP-505、ERP-808）。Owner：模块④。

    COLS = [Col('code', '商品编码'), Col('name', '商品名称', required=True), Col('retail', '零售价', kind='money'),
            Col('status', '状态', kind='enum', choices=['在售', '停售'])]
    send_template(COLS, '商品导入模板.xlsx', sample=[{...}])        # 下载模板（第二个 sheet 是字段说明）
    rows, errors = read_rows(request.files['file'], COLS)            # rows=[{'row_no':2,'data':{...}}]，errors=[{'row_no','name','errors':[..]}]
    return ok(import_result(created, updated, errors + biz_errors))  # 部分成功也给明确清单
    return send_xlsx(headers, rows, '商品.xlsx')                     # 导出（调用方负责权限 + audit）
金额列 kind='money' 读出来是 int 分；date 是 'YYYY-MM-DD'；int 是 int；decimal 是 Decimal。
"""
import io
from datetime import datetime, date
from decimal import Decimal, InvalidOperation

from flask import send_file
from openpyxl import Workbook, load_workbook

from .money import to_cents, MoneyError
from .util import BizError

MAX_IMPORT_ROWS = 20000
MAX_SYNC_EXPORT_ROWS = 50000     # 超过请走 export_jobs 异步导出（模块④）
XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


class Col(object):
    def __init__(self, key, title, required=False, kind='str', choices=None, max_len=200, note='', aliases=()):
        self.key, self.title, self.required, self.kind = key, title, required, kind
        self.choices, self.max_len, self.note, self.aliases = choices, max_len, note, tuple(aliases)


def _wb_bytes(wb):
    b = io.BytesIO()
    wb.save(b)
    b.seek(0)
    return b


def template_bytes(cols, sample=None, sheet='导入模板'):
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append([c.title + ('*' if c.required else '') for c in cols])
    for r in (sample or []):
        ws.append([r.get(c.key, '') for c in cols])
    doc = wb.create_sheet('字段说明')
    doc.append(['列名', '必填', '类型', '可选值', '说明'])
    kinds = {'str': '文本', 'int': '整数', 'money': '金额（元，两位小数）', 'date': '日期 YYYY-MM-DD', 'enum': '枚举', 'decimal': '小数', 'bool': '是/否'}
    for c in cols:
        doc.append([c.title, '是' if c.required else '', kinds.get(c.kind, c.kind), '、'.join(c.choices or []), c.note])
    return _wb_bytes(wb)


def send_template(cols, filename, sample=None):
    return send_file(template_bytes(cols, sample), mimetype=XLSX_MIME, as_attachment=True, download_name=filename)


def _conv(c, v):
    if v is None or (isinstance(v, str) and not v.strip()):
        if c.required:
            raise ValueError('%s 必填' % c.title)
        return None
    if c.kind == 'str':
        s = str(v).strip()
        if isinstance(v, float) and v.is_integer():
            s = str(int(v))                       # Excel 把条码读成 6.9e12 的情况
        if len(s) > c.max_len:
            raise ValueError('%s 超过 %d 字' % (c.title, c.max_len))
        return s
    if c.kind == 'int':
        try:
            d = Decimal(str(v).strip())
        except InvalidOperation:
            raise ValueError('%s 应为整数' % c.title)
        if d != d.to_integral_value():
            raise ValueError('%s 应为整数' % c.title)
        return int(d)
    if c.kind == 'decimal':
        try:
            return Decimal(str(v).strip())
        except InvalidOperation:
            raise ValueError('%s 应为数字' % c.title)
    if c.kind == 'money':
        try:
            return to_cents(v)
        except MoneyError:
            raise ValueError('%s 金额格式不正确' % c.title)
    if c.kind == 'date':
        if isinstance(v, (datetime, date)):
            return v.strftime('%Y-%m-%d')
        s = str(v).strip().replace('/', '-').replace('.', '-')
        try:
            return datetime.strptime(s[:10], '%Y-%m-%d').strftime('%Y-%m-%d')
        except ValueError:
            raise ValueError('%s 日期应为 YYYY-MM-DD' % c.title)
    if c.kind == 'bool':
        s = str(v).strip()
        if s in ('是', '1', 'Y', 'y', 'true', 'TRUE', '启用'):
            return True
        if s in ('否', '0', 'N', 'n', 'false', 'FALSE', '停用'):
            return False
        raise ValueError('%s 只能填 是/否' % c.title)
    if c.kind == 'enum':
        s = str(v).strip()
        if s not in (c.choices or []):
            raise ValueError('%s 只能是：%s' % (c.title, '、'.join(c.choices or [])))
        return s
    raise ValueError('未知列类型 %s' % c.kind)


def read_rows(file_storage, cols, max_rows=MAX_IMPORT_ROWS):
    """读第一个 sheet：按表头名称（去掉 * ）匹配列，顺序随意。返回 (rows, errors)。"""
    if file_storage is None:
        raise BizError('请上传 Excel 文件')
    try:
        data = file_storage.read() if hasattr(file_storage, 'read') else file_storage
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        raise BizError('文件不是有效的 .xlsx')
    ws = wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    try:
        head = [str(h or '').strip().rstrip('*') for h in next(it)]
    except StopIteration:
        raise BizError('文件是空的')
    idx = {}
    for c in cols:
        for name in (c.title,) + c.aliases:
            if name in head:
                idx[c.key] = head.index(name)
                break
    miss = [c.title for c in cols if c.required and c.key not in idx]
    if miss:
        raise BizError('模板缺少必填列：%s' % '、'.join(miss))
    rows, errors = [], []
    for n, raw in enumerate(it, start=2):
        if raw is None or all(v is None or str(v).strip() == '' for v in raw):
            continue
        if len(rows) + len(errors) >= max_rows:
            raise BizError('一次最多导入 %d 行' % max_rows)
        d, errs = {}, []
        for c in cols:
            v = raw[idx[c.key]] if c.key in idx and idx[c.key] < len(raw) else None
            try:
                d[c.key] = _conv(c, v)
            except ValueError as e:
                errs.append(str(e))
        if errs:
            errors.append({'row_no': n, 'name': d.get('name') or '', 'errors': errs})
        else:
            rows.append({'row_no': n, 'data': d})
    return rows, errors


def import_result(created=0, updated=0, errors=None, skipped=0):
    errors = sorted(errors or [], key=lambda e: e['row_no'])
    return {'created': created, 'updated': updated, 'skipped': skipped, 'error_count': len(errors), 'errors': errors}


def send_xlsx(headers, rows, filename, sheet='Sheet1'):
    if len(rows) > MAX_SYNC_EXPORT_ROWS:
        raise BizError('数据超过 %d 行，请缩小范围或使用异步导出' % MAX_SYNC_EXPORT_ROWS)
    wb = Workbook(write_only=True)
    ws = wb.create_sheet(sheet)
    ws.append(list(headers))
    for r in rows:
        ws.append(list(r))
    return send_file(_wb_bytes(wb), mimetype=XLSX_MIME, as_attachment=True, download_name=filename)


# ================================================================ 标准导入模板（DATA-001：模板 + 字段说明；各模块导入接口按同一列定义校验）
# kind: (模板名, 下载所需权限码, 列定义, 示例行, 导入接口（相对路径，由对应模块实现）)
# 模块①②的导入接口可直接 `from .xlsx_io import TEMPLATES; read_rows(f, TEMPLATES['goods'][2])`，保证模板与校验一致。
TEMPLATES = {
    'goods': ('商品档案导入模板', 'goods.import', [
        Col('code', '商品编码', note='留空自动生成；已存在则更新'), Col('barcode', '条码', note='多个用英文逗号分隔；商户内不能重复'),
        Col('name', '商品名称', required=True, max_len=60), Col('short_name', '简称', max_len=20), Col('spu_code', 'SPU', max_len=40),
        Col('category', '分类', max_len=30), Col('brand', '品牌', max_len=30), Col('spec', '规格', max_len=40, note='段位/尺码/容量等'),
        Col('unit', '基本单位', required=True, max_len=6, note='库存按基本单位记，如 罐/包/片'),
        Col('retail', '零售价', kind='money', required=True), Col('member_price', '会员价', kind='money'), Col('cost', '成本价', kind='money'),
        Col('batch_managed', '批次管理', kind='bool', note='奶粉、辅食等填 是'), Col('shelf_life_days', '保质期天数', kind='int'),
        Col('allow_discount', '允许折扣', kind='bool'), Col('allow_points', '允许积分', kind='bool'),
        Col('status', '状态', kind='enum', choices=['在售', '停售'])],
        [{'code': 'XB000001', 'barcode': '6900000000011', 'name': '示例奶粉一段800g', 'category': '奶粉', 'brand': '示例品牌', 'spec': '一段 800g',
          'unit': '罐', 'retail': '398.00', 'member_price': '388.00', 'cost': '300.00', 'batch_managed': '是', 'shelf_life_days': 730,
          'allow_discount': '是', 'allow_points': '是', 'status': '在售'}], 'api/goods/import'),
    'barcode': ('条码/箱码导入模板', 'goods.import', [
        Col('code', '商品编码', required=True), Col('barcode', '条码', required=True, max_len=40), Col('unit', '单位', required=True, max_len=6),
        Col('rate', '换算率', kind='int', required=True, note='1 箱 = 6 罐 填 6；基本单位条码填 1')],
        [{'code': 'XB000001', 'barcode': '16900000000018', 'unit': '箱', 'rate': 6}], 'api/goods/import'),
    'category': ('商品分类导入模板', 'goods.edit', [
        Col('code', '分类编码', required=True, max_len=20), Col('name', '分类名称', required=True, max_len=30), Col('parent_code', '上级分类编码', max_len=20)],
        [{'code': 'NF', 'name': '奶粉'}, {'code': 'NF1', 'name': '一段奶粉', 'parent_code': 'NF'}], 'api/goods/categories'),
    'member': ('会员导入模板', 'member.import', [
        Col('phone', '手机号', required=True, max_len=11, note='商户内唯一，重复会提示'), Col('name', '姓名', max_len=30),
        Col('code', '会员号', note='留空自动生成'), Col('card_no', '实体卡号', max_len=30), Col('level', '等级', max_len=20),
        Col('points', '积分', kind='int'), Col('stored', '储值余额', kind='money', note='迁移储值须与原系统核对后导入'),
        Col('reg_store_code', '注册门店编码', max_len=12), Col('baby_birthday', '宝宝生日/预产期', kind='date'),
        Col('baby_gender', '宝宝性别', kind='enum', choices=['男', '女', '未知'])],
        [{'phone': '13800000000', 'name': '张妈妈', 'level': '普通会员', 'points': 0, 'reg_store_code': '8007', 'baby_birthday': '2025-05-01',
          'baby_gender': '女'}], 'api/member/import'),
    'opening_stock': ('期初库存导入模板', 'stock.adjust', [
        Col('wh_code', '门店/仓库编码', required=True, max_len=12), Col('code', '商品编码', required=True),
        Col('qty', '数量（基本单位）', kind='int', required=True), Col('batch_no', '批号', max_len=40, note='批次管理商品必填'),
        Col('prod_date', '生产日期', kind='date'), Col('expiry_date', '有效期至', kind='date'), Col('cost', '成本价', kind='money')],
        [{'wh_code': '8007', 'code': 'XB000001', 'qty': 12, 'batch_no': 'B20260101', 'expiry_date': '2027-12-31', 'cost': '300.00'}],
        'api/stock/opening/import'),
    'stores': ('门店导入模板', 'org.edit', [
        Col('code', '门店编码', required=True, max_len=12, note='2-12 位字母数字；已存在则更新'), Col('name', '门店名称', required=True, max_len=40),
        Col('region_code', '区域编码', max_len=20), Col('store_type', '类型', kind='enum', choices=['直营', '加盟']),
        Col('address', '地址', max_len=100), Col('phone', '电话', max_len=30), Col('manager_name', '负责人', max_len=20),
        Col('open_hours', '营业时间', max_len=40), Col('status', '状态', kind='enum', choices=['营业', '停业'])],
        [{'code': '8007', 'name': '示例门店', 'store_type': '直营', 'address': '某某路 1 号', 'phone': '0000-0000000', 'manager_name': '张店长',
          'open_hours': '09:00-21:30', 'status': '营业'}], 'api/sys/import/stores'),
}
