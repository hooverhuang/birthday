async function refreshState() {
  let res = await fetch("/state");
  let state = await res.json();

  let playersDiv = document.getElementById("players");
  playersDiv.innerHTML = "<h2>玩家狀態</h2>";
  for (let [name, info] of Object.entries(state.players)) {
    playersDiv.innerHTML += `<p>${name} - 分數: ${info.score} - 角色: ${info.roles.join(", ")}</p>`;
  }

  let logsDiv = document.getElementById("logs");
  logsDiv.innerHTML = "<h2>遊戲紀錄</h2>" + state.logs.map(l => `<p>${l}</p>`).join("");
}

setInterval(refreshState, 2000);
refreshState();
