/* 游戏术语热词表（主进程侧，直连模式注入 session.update corpus.phrases）。
 * 数据与服务端 server/game_terms.json 同源；服务器模式下以服务端（管理后台可编辑）为准。 */
const TERMS = require('./game_terms.json');

function termsForTarget(lang) {
  if (lang === 'zh') return TERMS.en2zh;
  if (lang === 'en') return TERMS.zh2en;
  return {};
}

module.exports = { termsForTarget };
