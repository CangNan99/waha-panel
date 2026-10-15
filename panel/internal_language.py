"""Shared conservative rules for Chinese internal notes and AI labels."""
import json
import re
import unicodedata

HAN_PATTERN = r'[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0002fa1f]'
HAN = re.compile(HAN_PATTERN)
URL_PATTERN = r'https?://[^\s<>"\'，。；！？]+'
URL = re.compile(URL_PATTERN, re.I)
EMAIL_PATTERN = r'[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
WORD_PATTERN = r'[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ0-9_.+-]*'
NAMED_PATTERN = r'(?:姓名|联系人|品牌|型号|SKU|编号|产品|订单)\s*[:：]?\s*$'
ENTITY_PATTERN = r'((?:姓名|联系人|品牌|型号|SKU|编号|产品|订单)\s*[:：]?\s*)([A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ0-9_.+-]*(?: +[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ0-9_.+-]*){0,3})'
CODE_PATTERN = r'[A-Za-z]{1,8}[-_.]?\d+[A-Za-z]{0,4}(?:[-_.][A-Za-z0-9]{1,12})*'
WORDS = re.compile(WORD_PATTERN)
NAMED = re.compile(NAMED_PATTERN, re.I)
ALLOWED_WORDS = frozenset(x.lower() for x in (
    'WhatsApp','WAHA','VIP','SKU','AI','iPhone','iPad','MacBook','Pro','Max','Plus','Mini',
    'USB','Type-C','HDMI','WiFi','GB','TB','MB','kg','cm','mm','USD','EUR','CNY','PayPal',
    'Shopify','WooCommerce','Samsung','Apple','OpenAI','DeepSeek','ChatGPT','PDF','DOCX','JSON',
))
SENTENCE_WORDS = frozenset('a an the i we you he she it they this that these those is are was were be do does did not no please send contact quote need want wants hello hola necesito precio un una el la le les de des merci bonjour'.split())
PROPER_WORD_PATTERN = r'[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ]*'


def _entity_is_name(prefix, value):
    tokens = value.split()
    if any(token.lower() in SENTENCE_WORDS for token in tokens):
        return False
    unknown = [token for token in tokens if token.lower() not in ALLOWED_WORDS and not re.fullmatch(CODE_PATTERN, token)]
    # Multiple unknown words are reserved for explicitly identified people.
    # A product may contain one named brand plus known modifiers/model codes.
    return (len(unknown) <= 1 or re.search(r'姓名|联系人', prefix)) and all(re.fullmatch(PROPER_WORD_PATTERN, token) for token in unknown)


class InternalLanguageError(ValueError):
    pass


def validate_internal_chinese(text, kind='note'):
    if not isinstance(text,str):
        raise InternalLanguageError('内部资料必须为中文文本')
    original=text.strip()
    value=unicodedata.normalize('NFKC',original)
    if kind not in {'note','summary','label'}:
        raise ValueError('内部资料类型无效')
    if not value and kind=='note':
        return ''
    limit={'note':1000,'summary':4000,'label':100}[kind]
    if len(value)>limit:
        raise InternalLanguageError(f'内容不能超过 {limit} 个字符')
    rest=URL.sub(' ',value)
    rest=re.sub(EMAIL_PATTERN,' ',rest)
    rest=re.sub(ENTITY_PATTERN,lambda match:match.group(1)+' ' if _entity_is_name(match.group(1),match.group(2)) else match.group(),rest,flags=re.I)
    if not HAN.search(rest):
        raise InternalLanguageError('请使用中文填写内部资料')
    def remove_allowed(match):
        token=match.group().rstrip('.')
        if token.lower() in ALLOWED_WORDS or re.fullmatch(CODE_PATTERN,token):
            return ' '
        raise InternalLanguageError('请使用中文叙述；外语姓名、品牌或型号请注明用途')
    remainder=WORDS.sub(remove_allowed,rest)
    remainder=HAN.sub('',remainder)
    if any(char.isalpha() for char in remainder):
        raise InternalLanguageError('请将外语说明转换为中文后保存')
    return original


def frontend_validator_source():
    # Only fixed data is embedded. No user text or credentials become script code.
    return '''function isInternalChinese(text,kind='note'){
      if(typeof text!=='string')return false;
      const value=text.normalize('NFKC').trim();if(!value&&kind==='note')return true;
      if(value.length>({note:1000,summary:4000,label:100}[kind]||1000))return false;
      let rest=value.replace(/https?:\\/\\/[^\\s<>"'，。；！？]+/gi,' ').replace(/[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}/g,' ');
      const allowed=new Set(''' + json.dumps(sorted(ALLOWED_WORDS)) + ''');
      const sentenceWords=new Set(''' + json.dumps(sorted(SENTENCE_WORDS)) + ''');
      const entityIsName=(prefix,value)=>{const tokens=value.split(/ +/);if(tokens.some(token=>sentenceWords.has(token.toLowerCase())))return false;const unknown=tokens.filter(token=>!allowed.has(token.toLowerCase())&&!/^[A-Za-z]{1,8}[-_.]?\\d+[A-Za-z]{0,4}(?:[-_.][A-Za-z0-9]{1,12})*$/.test(token));return (unknown.length<=1||/姓名|联系人/.test(prefix))&&unknown.every(token=>/^[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ]*$/.test(token));};
      rest=rest.replace(/((?:姓名|联系人|品牌|型号|SKU|编号|产品|订单)\\s*[:：]?\\s*)([A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ0-9_.+-]*(?: +[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ0-9_.+-]*){0,3})/gi,(full,prefix,name)=>entityIsName(prefix,name)?prefix+' ':full);
      if(!/[\\u3400-\\u4dbf\\u4e00-\\u9fff\\u{20000}-\\u{2fa1f}]/u.test(rest))return false;
      let valid=true;
      rest=rest.replace(/[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ0-9_.+-]*/g,word=>{const token=word.replace(/\\.+$/,'');if(allowed.has(token.toLowerCase())||/^[A-Za-z]{1,8}[-_.]?\\d+[A-Za-z]{0,4}(?:[-_.][A-Za-z0-9]{1,12})*$/.test(token))return ' ';valid=false;return word;});
      rest=rest.replace(/[\\u3400-\\u4dbf\\u4e00-\\u9fff\\u{20000}-\\u{2fa1f}]/gu,'');return valid&&!/\\p{L}/u.test(rest);
    }'''
