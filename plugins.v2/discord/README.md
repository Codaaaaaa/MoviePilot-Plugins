### 配置

目前只支持webhook的方式，需要在discord服务器中创建一个webhook，然后将webhook的url填入插件配置中。
站点地址可以自行填写，也可以留空。如果填写了站点地址，那么在discord中点击消息时会跳转到相应页面。

跟原版一样支持各类消息通知开关。

新增discord bot，需要自行注册一个bot并获取token然后添加到自己服务器上。
步骤可参考 https://hackmd.io/@lio2619/B1PB2yB2c
需要注意添加以下权限：
- bot
- messages.read
- application.commands
嫌麻烦可以都勾上

目前指令支持：
- 订阅 /subscribe：输入片名时会自动补全候选；结果面板只有自己可见，可切换结果、选择剧集的季，并显示是否已订阅/已入库；订阅成功后会在频道里公开提示

直接@bot会使用GPT进行聊天

### 已知BUG

~~- 当MP因为更新重启后，Discord插件会无法载入，需要再手动重启一次MP。~~
- 未设置GPT回应对应用户/频道，导致bot一直跟webhook消息聊天。
- GPT对话过长未处理，导致消息发送失败。
~~- 莫名其妙错误导致GPT对话无法使用（在长时间开启对话后，大约一个礼拜？）~~

### Todo

- [X] 添加discord bot支持
- [X] 支持使用GPT聊天以及交互
- [X] 支持从discord直接发送命令