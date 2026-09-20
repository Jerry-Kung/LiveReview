/**
 * 前端测试的公共夹具。
 *
 * 单独成文件是为了让「已登录」这件事只有一个来源：V0.5 起工作台只在登录后渲染，
 * 每个测试文件都自己拼一份会话对象，改字段时必然会漏掉一处。
 */

/** 已登录的会话响应体，形状与 `GET /api/auth/session` 一致。 */
export const SESSION = {
  user: { username: "tester", display_name: "测试用户" },
} as const;

/**
 * 判断某个 URL 是否是会话查询。
 *
 * 单独抽出来是因为它在每个 mock 路由表里都要用，而路径写错的表现是「测试停在载入中」，
 * 排查起来比多一层函数调用麻烦得多。
 */
export function sessionRoute(url: string, body: unknown = SESSION) {
  return url === "/api/auth/session" ? body : undefined;
}
