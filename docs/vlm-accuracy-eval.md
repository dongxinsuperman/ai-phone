# VLM 准确性测试集

这是一套不操作真机的可重复评测：它生成固定手机界面，给每个目标元素标注精确矩形区域，再通过生产同款 `VLMClient` 请求模型。

它测的是可观察行为，不要求或评分模型的内部思维过程：动作名、归一化坐标、滑动方向、文本内容、完成/失败判断，以及多轮状态续接。

## 覆盖范围

- 6 个坐标点击：左上、右上、中心、左下、右下、重复文案 + 颜色区分；
- 4 个语义判断：向下浏览、已聚焦输入、已有完成证据、可见断言失败；
- 1 个三步状态流：`NEXT` → `SAVE` → 成功页 `finished`。

坐标分数以 AI Phone 的 `0-1000` 坐标系计算。落在目标矩形内或距边界不超过 42 个归一化单位才算命中；这个容差是为了评估模型定位而非要求像素级点击。

## 先审查，不花模型额度

```bash
cd backend
.venv/bin/python scripts/run_vlm_accuracy_eval.py --list
```

## 运行新接入点评测

不要把 Key 写进命令行或提交到仓库；只在当前 shell 设置临时变量：

```bash
cd backend
export VLM_EVAL_API_KEY='<new-key>'
.venv/bin/python scripts/run_vlm_accuracy_eval.py \
  --model ep-20260715213839-t47qx \
  --output ../.data/vlm-evals/new-model.json
```

如运行环境对单个命令有时间上限，可按案例分批运行；各批报告可独立保存：

```bash
.venv/bin/python scripts/run_vlm_accuracy_eval.py \
  --model ep-20260715213839-t47qx \
  --case coord_top_left --case coord_top_right \
  --output ../.data/vlm-evals/coordinates-part-1.json
```

报告会给出整体通过率、坐标命中率、每一步模型原始输出、解析后的动作和延迟。比较旧/新模型时，应使用同一份 suite，各运行至少三次，再比较中位数而不是单次平均值。

## 边界

该测试集是“受控基准”：它能稳定发现坐标系错位、动作协议不兼容、状态判断倒退等问题，但不能替代真实 App 的端到端回归。模型通过该套件后，仍要在真实设备上跑少量高风险业务路径（登录、输入、滚动、提交、断言）。
