# GitHub App 권한 갱신 (이미 만들어진 App)

manifest(`backend/connectors/auth/github_manifest.py` 의 `_DEFAULT_PERMISSIONS`)는 App 을
**처음 만들 때만** 적용된다. manifest 가 바뀌어도 이미 만들어진 App 의 권한은 그대로다.

## 증상

PR 을 연 뒤 원래 이슈에 코멘트가 달리지 않고 로그에 다음이 남는다.

```
github_app_issues_write_permission_missing   (워크스페이스당 한 번)
github_delivery_issue_comment_failed ... 403 Resource not accessible by integration
```

## 절차

1. GitHub → Settings → Developer settings → GitHub Apps → 해당 App → **Permissions & events**.
2. Repository permissions 의 **Issues** 를 `Read and write` 로 바꾸고 저장한다.
3. App 이 설치된 org/계정의 Installation 페이지에서 **권한 변경 요청을 승인**한다
   (승인 전에는 설치 토큰에 새 권한이 반영되지 않는다).
4. 다음 배송부터 코멘트가 달린다. 재연결은 필요 없다.

새로 만드는 App 은 manifest 가 `issues: write` 를 요청하므로 이 절차가 필요 없다.
