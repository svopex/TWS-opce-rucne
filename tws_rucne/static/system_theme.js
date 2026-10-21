// Systémová preference tmavého vzhledu. Server se na ni ptá při připojení
// (theme.py) a o každé další změně se dozví událostí - v režimu „podle
// systému" podle ní barví grafy, protože canvas CSS třídu body--dark nevidí.
const systemDarkQuery = window.matchMedia("(prefers-color-scheme: dark)");

systemDarkQuery.addEventListener("change", (e) => emitEvent("system_dark", e.matches));
