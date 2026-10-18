import re, pathlib
P = pathlib.Path('/Users/suenot/projects/sdvg/gaia/prompts')
O = P / 'i18n'
EXTRA = {  # collections whose songs were supplied by the user: style written by hand
 'statistical-arbitrage-pairs': ("Length: 0:29. A very short 29-second jingle for a children's cartoon about a pianist cat and two penguins on a vaudeville stage.",
   "Ragtime vaudeville with honky-tonk piano, banjo, tuba, brushed drums and a bouncy male crooner, 150 BPM: no long intro, vocals start at once, one chorus, then a big ending hit."),
 'onchain-defi-quant': ("Length: 0:30. A very short 30-second jingle for a children's cartoon about a hamster chef running a busy diner.",
   "Vaudeville revival swing with tuba, clarinet, banjo, stomping drums and a theatrical male crooner, 150 BPM: no long intro, vocals start at once, one chorus, then a big ending hit."),
}
EN_FILE = {'ai-agents-trading':'mm_ai-agents-trading_short.txt','causality-lead-lag':'mm_causality-lead-lag_short.txt',
 'complex-arbitrage-rust':'mm_complex_arbitrage_rust_short.txt','execution-market-making':'mm_execution-market-making_short.txt',
 'marketmaker-general':'mm_marketmaker-general_short.txt','modern-ml-trading':'mm_modern-ml-trading_short.txt',
 'order-book-microstructure':'mm_order-book-microstructure_short.txt','volatility-regimes':'mm_volatility-regimes_short.txt'}
L = {
'complex-arbitrage-rust': dict(
 ru=("Краб-арбитражник","Раз-два, цикл плывет,\nГраф и матрица ведут,\nСпоты, фьючи, море цен,\nКраб-капитан, держи курс!"),
 zh=("套利螃蟹","嘿哟嘿哟，环路转，\n图和矩阵，铁锈蟹最懂，\n期货现货，连成海，\n复杂套利，跟我出航！")),
'ai-agents-trading': dict(
 ru=("ИИ-агенты для трейдинга","Бип-боп, сигнал идет,\nАгент торгует круглый год,\nСмотрит, решает и снова в бой,\nИИ-агенты, мы с тобой!"),
 zh=("交易智能体","哔哔啵啵，信号跑，\n智能体交易不睡觉，\n观察，决策，再行动，\n交易智能体，出发啦！")),
'causality-lead-lag': dict(
 ru=("Причинность и лид-лаг на рынках","Лидер, лаг, шаг за шагом,\nИдем по следу рынка,\nНайди причину, проверь дважды,\nПричинность на рынках!"),
 zh=("市场因果与领先滞后","领先，滞后，一步一步，\n跟着市场留下的脚印，\n找出原因，验证两次，\n市场因果与领先滞后！")),
'execution-market-making': dict(
 ru=("Исполнение и маркет-мейкинг","Чух-чух, проскальзывай тише,\nБид покупай, аск продавай,\nЗапас под контролем, все в порядке,\nИсполнение и маркет-мейкинг, вперед!"),
 zh=("执行与做市","呜呜呜，滑点要低，\n买在买价，卖在卖价，\n库存管好，一切有序，\n执行与做市，出发！")),
'marketmaker-general': dict(
 ru=("Восемь рук на стакане","Купи дешево, продай дорого,\nКотируй обе стороны, рыбки плывут,\nУзкий спред, твердая рука,\nМаркетмейкер! Маркетмейкер из песка!"),
 zh=("八只手在订单簿上","低买高卖，\n双边报价，鱼儿游过，\n价差很窄，手很稳，\n做市商！珊瑚礁的做市商！")),
'modern-ml-trading': dict(
 ru=("Современное МО для трейдинга","Подгоняй точно, не переобучай,\nПроверяй каждый кусочек,\nДеревья и сети учатся и растут,\nСовременное МО для трейдинга, твой выход!"),
 zh=("现代机器学习交易","拟合刚好，别过拟合，\n每一步都要验证，\n树和网络，学习成长，\n现代机器学习，交易之星！")),
'order-book-microstructure': dict(
 ru=("Стакан и микроструктура рынка","Эй! Спред так узок,\nГлубина и поток слева направо,\nКаждый тик и каждая очередь,\nСтакан и микроструктура рынка!"),
 zh=("订单簿与市场微观结构","嘿！价差很窄，\n深度与订单流，从左到右，\n每个报价，每个队列，\n订单簿与市场微观结构！")),
'volatility-regimes': dict(
 ru=("Моделирование волатильности и режимы","Лови штиль, лови шторм,\nУ каждого режима своя форма,\nМеряй волну, читай море,\nМоделирование волатильности и режимы!"),
 zh=("波动率建模与市场状态","乘着平静，乘着风暴，\n每种状态各有模样，\n测量浪涌，读懂大海，\n波动率建模与市场状态！")),
'statistical-arbitrage-pairs': dict(
 ru=("Танец спреда","Две акции зашли в график, ха!\nТанцуют рядом, потом врозь,\nКупи дешевую, продай дорогую,\nСпред наша мелодия,\nТоргуй спредом, торгуй спредом,\nРасстояние шепчет: быть вместе,\nКоинтеграция бьет в колокол,\nФильтр Калмана знает толк."),
 zh=("价差舞步","两只股票走进图表，哈！\n时而靠近，时而分开，\n买便宜的，卖贵的，\n价差就是我们的曲调，\n交易价差，交易价差，\n距离说它们该结婚，\n协整敲响钟声，\n卡尔曼滤波最清楚。")),
'onchain-defi-quant': dict(
 ru=("Сэндвич-гамбит","Стакан на втором уровне,\nБиды и аски как на ладони,\nMEV крадется в мемпуле тьмы,\nСэндвич-атаки оставляют след,\nЧитай стакан, читай стакан,\nМикроструктура не уйдет,\nЗвенит звонок матчинга,\nЗадержка знает толк."),
 zh=("三明治棋局","订单簿第二层，\n买卖报价一目了然，\nMEV潜伏在内存池的暗处，\n三明治攻击留下印记，\n读懂订单簿，读懂订单簿，\n市场微观结构跑不掉，\n撮合的铃声响起，\n延迟最懂它。")),
}
LANG = {'ru':'Russian','zh':'Mandarin Chinese'}
for c, d in L.items():
    if c in EXTRA: l1, l2 = EXTRA[c]
    else:
        t = (P/EN_FILE[c]).read_text().strip().splitlines()
        l1, l2 = t[0], t[1]
    for lg,(title,lyr) in d.items():
        l1x = l1.rstrip('.') + f". Sung in {LANG[lg]}."
        txt = f'{l1x}\n{l2}\n\nTitle: "{title}"\n\nLyrics (sing exactly these {LANG[lg]} lyrics, no English words):\n[Chorus]\n{lyr}\n'
        (O/f'{c}_{lg}.txt').write_text(txt)
print(sorted(x.name for x in O.glob('*.txt')))
