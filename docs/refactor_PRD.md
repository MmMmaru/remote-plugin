## 目标
重构仓库的远程开发环境、原理。
以人类的手工编写的machines.json为唯一根据

## 目前
目前项目环境已有harness，完全不依赖当前harness去实现开发流程
只用remote-plugin这个文件相关CLI。
机器注册及信息具体查看目前已有状态
目前remaining还有待执行的项目。

## 验收
完成.remote文件下文件对齐，目前没对齐。
在remote-plugin这个CLI下，跑通容器配置、源码同步、编译、起服务、拉取日志。
记录每个任务从开始到完成时间
### 验收1
跑通服务及curl通话, 保留日志
### 验收2
跑通服务并做profiling，analyze之后下载到本地
### 验收3
跑通服务并做benchmark，下载到本地结果。

## 运行时要求
代码commit并及时push到远程
模型、配置自己决定，不需要问我。
