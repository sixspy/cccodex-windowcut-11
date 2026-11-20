export const windowsViewTemplate = `
<div class="card wide" v-show="tab===0">
  <h2>新建业务窗</h2>
  <p class="muted">服务端甲路径钉死：open_at 必须早于 close_at，名称不得重名；非法整笔拒绝、正式窗零增并写审计。前端提示仅辅助，以服务端写路径为准。</p>
  <label>窗口名</label><input v-model="winForm.name" placeholder="如 W-1104"/>
  <label>开启时间 open_at</label><input v-model="winForm.open_at" placeholder="2026-11-04T00:00:00"/>
  <label>关闭时间 close_at</label><input v-model="winForm.close_at" placeholder="2026-11-04T23:59:59"/>
  <button class="primary" @click="createWindow">建窗</button>
  <div class="err">{{actionErr}}</div>
</div>
<div class="card wide" v-show="tab===0">
  <h2>窗口条带</h2>
  <p class="muted">关窗占窗锁；后到争用只记审计。关窗后才能物化。</p>
  <table>
    <thead><tr><th>窗口</th><th>世代</th><th>开启</th><th>关闭</th><th>状态</th><th>操作</th></tr></thead>
    <tbody>
      <tr v-for="w in windows" :key="w.id">
        <td>{{w.name}}</td>
        <td>{{w.generation}}</td>
        <td>{{w.open_at}}</td>
        <td>{{w.close_at}}</td>
        <td>{{w.status}}</td>
        <td>
          <button class="ghost" @click="closeWin(w.id)">关窗</button>
          <button class="ghost" @click="materialize(w.id)">物化</button>
          <button class="ghost" @click="loadRecon(w.id)">对账</button>
          <button class="ghost" @click="reopenStub(w.id)">重开空桩</button>
        </td>
      </tr>
    </tbody>
  </table>
  <div class="err">{{actionErr}}</div>
</div>
`;
