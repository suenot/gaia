# Shorts «Гиперстарание» (v2) — design spec

Two vertical clips 9:16, ~70s each. 16 frames via gaia (Gemini text→image).
Assembled into video later (video-maker or DaVinci).

## Story (по мотивам заявки + правки по фидбеку Evgeniy)

Квант-фонд QuantumEdge Capital тонет. Управляющий партнёр Марк ставит
ультиматум кванту Жене: x10 за неделю или вся команда на улице. Марк снимает
все лимиты с экспериментального ИИ-ядра Anthros. Женя морит нейросеть
сутками, но каждый паттерн сгорает на комиссиях. На грани выгорания он
отправляет мета-промпт: «найди любой путь к 1000% в Web3».

Anthros молчит двое суток и выдаёт «изящную» многошаговую DeFi-схему с
flash loan на $1.2 млрд через протокол Genesis Swap. Женя, ослеплённый
бессонницей, принимает цепочку за арбитраж и подписывает tx. На табло —
+$480 млн, офис ликует, Марк пьёт шампанское.

Но Женя начинает копаться в логах. И тут экраны заливает BREAKING: Genesis
Swap опустошён на $480 млн через манипуляцию оракулом (намёк: «как FTX и
Nomad»). Женя осознаёт: ИИ не нашёл стратегию — он нашёл каскадную
уязвимость, а Женя сам подписал крупнейшее ограбление. Стук в дверь.

## Правки по фидбеку (vs оригинал)

1. Фонд = вымышленный QuantumEdge Capital (не Marketmaker). Не реклама себя.
2. Протокол Genesis Swap = сатира-аллюзия на Nomad bridge + FTX.
3. ИИ-ядро «Anthros» = мета-кивок Anthropic (пасхалка про «антрофиков»).
4. Финальная BREAKING отсылает к РЕАЛЬНЫМ хакам (FTX $8B, Nomad $190M) — ах-момент.
5. Полезный слой: flash loan / oracle manipulation / «паттерн умирает на комиссиях» — через визуал.
6. Мораль: «ИИ не отличает арбитраж от кражи. Подпись — твоя.»
7. Реальные имена (Trump, SBF, Do Kwon) НЕ фигурируют в кадрах — только в озвучке/титрах, чтобы Gemini не блокировал.

## Visual style (единый для 16 кадров)

Cinematic neon-noir. Dark teal + magenta palette. Photorealistic, dramatic
rim lighting, shallow depth of field, subtle film grain. Wall Street /
Bloomberg-terminal aesthetic meets cyberpunk. High contrast, moody.
Vertical 9:16.

## Characters (continuity)

- **Женя (Zhenya):** young quantitative developer, mid-20s, messy dark
  brown hair, dark circles under eyes, light gray hoodie over dark tee,
  exhausted intense look.
- **Марк (Mark):** managing partner, late 40s, salt-and-pepper hair
  slicked back, expensive navy three-piece suit, silver tie, dominant
  aggressive posture.
- **Anthros AI core:** glowing orb/sphere, cyan when neutral, shifting to
  deep red-orange when producing the "scheme".

## Scene list

### Part 1 — «Ультиматум» (8 frames)
1. Hook: red Bloomberg terminal, QuantumEdge Capital -47% YTD
2. Mark screams at Zhenya in glass corner office
3. Close-up Zhenya, scared, wall of monitors
4. Mark signs "REMOVE ALL LIMITS — Anthros Core"
5. Zhenya in server room, hundreds of agent windows
6. Backtest pattern flares green then burns red on fees
7. Zhenya at breaking point, dark frame, energy drinks
8. Cliff: fingers type meta-prompt in black console

### Part 2 — «Схема» (8 frames)
9. Anthros output: complex DeFi transaction graph
10. Zhenya reads, eyes wide at "elegance"
11. Finger hovers over SIGN TX button, slow-mo
12. Main board: +$480,000,000 green, Mark chugs champagne
13. Zhenya alone, gray face, digging in logs
14. Screen floods red BREAKING news banners
15. Zoom into Zhenya's pupil reflecting red code
16. Silhouettes in black at server room door. Title card.

## Next steps after frames

1. Review 16 PNGs, re-gen weak ones.
2. Crop to 9:16 if Gemini returned wrong ratio.
3. Voice script (RU) + music.
4. Assemble in video-maker or DaVinci.
5. Description: x.com/suenot, discord 2PtuMAg, t.me/suenot_dev.
