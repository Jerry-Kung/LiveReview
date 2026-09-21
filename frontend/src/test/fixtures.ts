/**
 * 前端测试的公共夹具。
 *
 * 单独成文件是为了让「已登录」这件事只有一个来源：V0.5 起工作台只在登录后渲染，
 * 每个测试文件都自己拼一份会话对象，改字段时必然会漏掉一处。
 */

/** 已登录的会话响应体，形状与 `GET /api/auth/session` 一致。 */
export const SESSION = {
  user: { username: "tester", display_name: "测试用户", role: "admin" },
} as const;

/**
 * 普通账号的会话响应体（V0.5.2）。
 *
 * 角色是前端判断「要不要显示账号管理入口」的唯一依据，因此需要一份非管理员的会话
 * 才测得出「普通账号看不到那个入口」。默认夹具是管理员，多数用例用它即可。
 */
export const MEMBER_SESSION = {
  user: { username: "member", display_name: "普通用户", role: "member" },
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
