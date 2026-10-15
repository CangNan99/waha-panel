try:
    from .visual_theme import SHARED_CSS
    from .overview_page import MONITOR_CSS
except ImportError:
    from visual_theme import SHARED_CSS
    from overview_page import MONITOR_CSS


def logs_page():
    return '<html><head><title>系统日志 · WhatsApp AI 管理面板</title><style>' + SHARED_CSS + MONITOR_CSS + '''</style></head><body>
<div class="monitoring-page" id="logsPage"><header class="page-heading"><div><h1>系统日志</h1><p>按会话、时间和事件查找处理记录。</p></div><div class="page-heading-tools"><button id="refreshSystemLogs" type="button">刷新日志</button></div></header>
<section class="monitor-card"><form id="logFilterForm"><div class="log-filter-grid">
<label class="control-label" for="logSession">会话范围<select id="logSession"><option value="all">全部会话</option></select></label>
<label class="control-label" for="logLevel">级别<select id="logLevel"><option value="">全部级别</option><option value="INFO">信息</option><option value="WARNING">警告</option><option value="ERROR">错误</option><option value="CRITICAL">严重错误</option><option value="DEBUG">调试</option></select></label>
<label class="control-label" for="logRange">时间范围<select id="logRange"><option value="1h">最近 1 小时</option><option value="24h" selected>最近 24 小时</option><option value="7d">最近 7 天</option><option value="custom">自定义</option></select></label>
<label class="control-label" for="logEvent">事件类型<input id="logEvent" maxlength="100" placeholder="例如 session.start"></label>
<label class="control-label search-field" for="logSearch">关键词<input id="logSearch" maxlength="100" placeholder="搜索已脱敏的日志"></label><button class="primary" id="applyLogFilters" type="submit">应用筛选</button>
</div><div class="log-custom-range" id="logCustomRange" hidden><label class="control-label" for="logFrom">开始时间（上海时间）<input id="logFrom" type="datetime-local"></label><label class="control-label" for="logTo">结束时间（上海时间）<input id="logTo" type="datetime-local"></label></div></form>
<div class="log-filter-footer"><label class="log-auto" for="logAutoRefresh"><input id="logAutoRefresh" type="checkbox">自动检查新日志（5 秒）</label><span class="log-updated" id="logUpdatedAt">尚未读取</span></div></section>
<div class="monitor-notice" id="systemLogNotice" role="status" aria-live="polite">正在读取日志…</div>
<button class="log-new-notice" id="newSystemLogs" type="button" hidden>有新日志 · 点击查看</button>
<section class="monitor-card"><div id="systemLogs" aria-live="off"></div><div class="log-pagination"><span id="systemLogCount">—</span><button id="loadOlderLogs" type="button" hidden>加载更早记录</button></div></section>
<dialog id="logDetailDialog" aria-labelledby="logDetailTitle"><div class="dialog-heading"><div><h2 id="logDetailTitle">日志详情</h2><p id="logDetailMeta"></p></div><button id="closeLogDetail" type="button" aria-label="关闭日志详情">关闭</button></div><div class="log-detail-body" id="logDetailBody"></div><div class="card-actions"><button id="copyLogDetail" type="button">复制脱敏内容</button><span id="logCopyStatus" role="status"></span></div></dialog>
</div></body></html>'''
