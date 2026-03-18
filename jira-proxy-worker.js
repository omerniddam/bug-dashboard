/**
 * Cloudflare Worker — Jira API Proxy for Bug Dashboard
 *
 * Secrets to configure in Cloudflare (Settings → Variables → Add):
 *   JIRA_URL        → https://oxsecurity.atlassian.net
 *   JIRA_EMAIL      → omer.niddam@ox.security
 *   JIRA_API_TOKEN  → your Atlassian API token
 *
 * Deploy once, paste the worker URL into GitHub Secrets as JIRA_PROXY_URL.
 */

const FIELDS = 'summary,status,assignee,priority,created,resolutiondate,labels,issuetype,customfield_10001,customfield_10032,customfield_10112';

const CORS_HEADERS = {
  'Access-Control-Allow-Origin':  '*',
  'Access-Control-Allow-Methods': 'GET, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
};

export default {
  async fetch(request, env) {
    // Handle CORS preflight
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    if (request.method !== 'GET') {
      return new Response('Method not allowed', { status: 405, headers: CORS_HEADERS });
    }

    const url      = new URL(request.url);
    const jql      = url.searchParams.get('jql');
    const startAt  = url.searchParams.get('startAt') || '0';
    const maxResults = url.searchParams.get('maxResults') || '100';

    if (!jql) {
      return new Response(JSON.stringify({ error: 'Missing jql parameter' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json', ...CORS_HEADERS },
      });
    }

    const jiraSearchUrl = env.JIRA_URL +
      '/rest/api/3/search?jql=' + encodeURIComponent(jql) +
      '&startAt=' + startAt +
      '&maxResults=' + maxResults +
      '&fields=' + FIELDS;

    const auth = btoa(env.JIRA_EMAIL + ':' + env.JIRA_API_TOKEN);

    let jiraRes;
    try {
      jiraRes = await fetch(jiraSearchUrl, {
        headers: {
          'Authorization': 'Basic ' + auth,
          'Accept': 'application/json',
        },
      });
    } catch (e) {
      return new Response(JSON.stringify({ error: 'Failed to reach Jira: ' + e.message }), {
        status: 502,
        headers: { 'Content-Type': 'application/json', ...CORS_HEADERS },
      });
    }

    const body = await jiraRes.text();
    return new Response(body, {
      status: jiraRes.status,
      headers: { 'Content-Type': 'application/json', ...CORS_HEADERS },
    });
  },
};
