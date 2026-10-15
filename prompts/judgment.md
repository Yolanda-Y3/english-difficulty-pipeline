# 判断执行校正：不改变当前rubric

只依据本次题型和当前维度的criterion、anchors作判断，不引入新阈值，不参考人工分数，也不预设本次分数应升高或降低。每个维度在同一次调用完成完整判断。

## 共用执行要求

- 先确定学生实际需要处理的题面证据，再选择已有档位。给定答案、解析可用于核对，但不能把解析已经说明的结论当成学生起点；写清尚未知答案的普通高中学生须识别什么。
- score_basis.anchor_match须明确指出当前分数对应的原始anchor，并解释事实如何满足它。不能自造“2—3个结构属于同一档”等原表未给的区间，不能借用其他题型的档位。逐项核对criterion的且、或、数量、范围条件。
- 在metrics.rule_application中输出{anchor_score,anchor_quote,criterion_evidence,adjacent_band_check}。anchor_score等于score，anchor_quote原样复制当前分数对应的anchors项；score=null时anchor_quote=null。其余两项用简洁文字说明事实与原判据的对应和相邻档为何不适用。不要只写“符合要求”。
- 若需计数，在metrics.judgment_items中逐项列出{id,label,refs,counted,reason}，counted为布尔值，说明该项为何独立计入或未计入；metrics.judgment_count等于counted=true项数。非计数维度输出空清单及0。判断是否计入由模型按原表负责，程序只核对列表与数字，不替代语义判断。
- 对同一结构或操作的不同称呼不要重复计数；不同的实际结构或判断不能仅因都在同一句、同一考点或同一大步骤而合并。原规则未明确的边界据实在reason记录，不自行新增定义或一律从高、从低。

## 完整输出

只输出schema字段。顶层sense_matches和notes不可遗漏；无词汇出现目标时sense_matches=[]。每条fact须有真实题面引用；非空score须有非空refs、evidence_ids、score_basis.refs。需要判断或计数的工作由当前节点完成，不能将“尚未分析”写成材料缺失。
facts使用f1、f2等编号，judgment_items使用j1、j2等独立编号。evidence_ids只能引用本次facts中的f编号，不能使用judgment_items的j编号。judgment_count必须是整数；非计数维度输出judgment_items=[]且judgment_count=0，不能遗漏。reason文字不代替refs；每个清单项需要原文引用。
清单每项只表示一个具体候选结构或一个具体判断，不列“简单句计数”“总数”“结构层级”这样的统计汇总项。总数只写入judgment_count。同类但不同位置的结构逐项列出，不把类型数或嵌套层数当作实际结构数量；不改变原表对哪些结构属于复杂结构的定义。
