export const cagesViewTemplate = `
<section class="panel">
  <h2>入笼冻结</h2>
  <p class="hint">从当事人可用保证金划入新笼，写入笼代。同合约多只冻结笼可在下方按甲路径（整笔同事务）收成一只主笼：同事务作废来源笼、累加金额、重算冻结、记审计；失败整笔回滚。</p>
  <div class="grid-2">
    <div>
      <div class="form-row">
        <div>
          <label>当事人键</label>
          <input v-model="freezeForm.party_key" list="party-list"/>
          <datalist id="party-list">
            <option v-for="b in bonds" :key="b.party_key" :value="b.party_key"/>
          </datalist>
        </div>
        <div>
          <label>合约号</label>
          <input v-model="freezeForm.contract_no"/>
        </div>
        <div>
          <label>金额</label>
          <input type="number" v-model.number="freezeForm.amount" min="1" step="1000"/>
        </div>
        <div>
          <label>受益人</label>
          <input v-model="freezeForm.beneficiary"/>
        </div>
        <div>
          <label>笼编码（可空自动）</label>
          <input v-model="freezeForm.cage_code" placeholder="CG-P-WEST-001"/>
        </div>
      </div>
      <div class="actions">
        <button @click="freezeCage">入笼冻结</button>
        <button class="secondary" @click="refresh">刷新</button>
      </div>
    </div>
    <div>
      <div class="cage-cards">
        <div
          class="cage-card"
          v-for="c in cages"
          :key="c.id"
          :class="{selected: selectedCage && selectedCage.id===c.id, void: c.status==='已合并作废'}"
          @click="selectCage(c)"
        >
          <div class="stamp-corner">{{ shortStatus(c.status) }}</div>
          <div class="code">{{ c.cage_code }}</div>
          <div class="amt">{{ fmt(c.available) }} / {{ fmt(c.amount) }}</div>
          <div>{{ c.contract_no }} · 代{{ c.generation }}</div>
          <div>{{ c.party_key }} → {{ c.beneficiary }}</div>
          <div v-if="c.status==='已合并作废'" class="void-tag">已合并作废</div>
        </div>
      </div>
    </div>
  </div>

  <h3 style="margin-top:16px">同约多笼合并（甲路径 · 整笔同事务）</h3>
  <p class="hint">口径当日锁死：仅「整笔同事务」；影子晋升等乙路径禁用，偏航拒写。后到争用只留痕、不写业务。</p>
  <div v-if="!mergeGroups.length" class="hint">暂无可合并组（需同当事人、同合约且 ≥2 只已冻结笼）。</div>
  <table v-for="g in mergeGroups" :key="g.key" class="merge-group">
    <thead>
      <tr>
        <th>主笼</th><th>笼编码</th><th>合约</th><th>当事人</th><th>金额</th><th>状态</th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="c in g.cages" :key="c.id">
        <td>
          <input
            type="radio"
            :name="'main-' + g.key"
            :value="c.id"
            :checked="g.main.id === c.id"
            @change="pickMergeMain(g.key, c.id)"
          />
        </td>
        <td>{{ c.cage_code }}</td>
        <td>{{ c.contract_no }}</td>
        <td>{{ c.party_key }}</td>
        <td>{{ fmt(c.amount) }}</td>
        <td><span class="badge frozen">{{ c.status }}</span></td>
      </tr>
    </tbody>
    <tfoot>
      <tr>
        <td colspan="6">
          主笼 <strong>{{ g.main.cage_code }}</strong> 承接来源 {{ g.sources.length }} 只 ·
          并入 {{ fmt(g.added) }} · 合并后主笼 {{ fmt(g.total) }}
          <button class="ok" style="margin-left:10px" @click="mergeGroup(g)">
            合并入 {{ g.main.cage_code }}
          </button>
        </td>
      </tr>
    </tfoot>
  </table>

  <h3 style="margin-top:16px">合并血缘</h3>
  <table v-if="mergeJobs.length">
    <thead>
      <tr><th>时间</th><th>合约</th><th>主笼ID</th><th>口径</th><th>合并金额</th><th>来源笼</th><th>状态</th></tr>
    </thead>
    <tbody>
      <template v-for="mj in mergeJobs" :key="mj.job.id">
        <tr v-for="(l, idx) in mj.lineage" :key="l.id">
          <td>{{ idx === 0 ? mj.job.created_at : '' }}</td>
          <td>{{ idx === 0 ? mj.job.contract_no : '' }}</td>
          <td>{{ idx === 0 ? mj.job.main_cage_id : '' }}</td>
          <td>{{ idx === 0 ? mj.job.mode : '' }}</td>
          <td>{{ idx === 0 ? fmt(mj.job.merged_amount) : '' }}</td>
          <td>{{ l.source_cage_code }} · {{ fmt(l.amount) }}</td>
          <td>{{ idx === 0 ? mj.job.status : '' }}</td>
        </tr>
      </template>
    </tbody>
  </table>
  <p v-else class="hint">尚无合并作业。</p>

  <div v-if="cageDetail">
    <h3>笼详情 · {{ cageDetail.cage.cage_code }}</h3>
    <table>
      <thead><tr><th>字段</th><th>值</th></tr></thead>
      <tbody>
        <tr><td>状态</td><td><span class="badge" :class="statusClass(cageDetail.cage.status)">{{ cageDetail.cage.status }}</span></td></tr>
        <tr><td>可用 / 总额</td><td>{{ fmt(cageDetail.cage.available) }} / {{ fmt(cageDetail.cage.amount) }}</td></tr>
        <tr><td>核验条数</td><td>{{ cageDetail.attestations.length }}</td></tr>
        <tr><td>拨付条数</td><td>{{ cageDetail.releases.length }}</td></tr>
        <tr><td>罚没条数</td><td>{{ cageDetail.forfeits.length }}</td></tr>
      </tbody>
    </table>
    <div v-if="cageDetail.merged_out && cageDetail.merged_out.length">
      <h4>本笼已并入</h4>
      <table>
        <thead><tr><th>时间</th><th>主笼</th><th>金额</th></tr></thead>
        <tbody>
          <tr v-for="m in cageDetail.merged_out" :key="m.id">
            <td>{{ m.merged_at }}</td><td>{{ m.main_cage_code }}</td><td>{{ fmt(m.amount) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-if="cageDetail.merged_in && cageDetail.merged_in.length">
      <h4>本笼承接的来源笼</h4>
      <table>
        <thead><tr><th>来源笼</th><th>金额</th><th>时间</th></tr></thead>
        <tbody>
          <tr v-for="m in cageDetail.merged_in" :key="m.id">
            <td>{{ m.source_cage_code }}</td><td>{{ fmt(m.source_amount) }}</td><td>{{ m.created_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</section>
`;
