# DigitalOcean Spaces cache

sub2gen can store generated media in either the local filesystem or a DigitalOcean Standard Space. Only one provider and one delivery mode are active at a time.

## Provisioning

1. Create a DigitalOcean Spaces Standard Storage bucket. sub2gen does not create buckets.
2. Keep bucket file listing restricted.
3. Create a Spaces access key with object read, write, delete, and ACL permissions.
4. If direct CDN delivery is needed, enable the Spaces CDN and copy its endpoint URL and endpoint ID.
5. For CDN purge support, create a scoped DigitalOcean API token with the CDN cache-delete permission.

Set the following deployment variables:

```env
SUB2GEN_DO_SPACES_ACCESS_KEY_ID=...
SUB2GEN_DO_SPACES_SECRET_ACCESS_KEY=...
SUB2GEN_DO_SPACES_REGION=nyc3
SUB2GEN_DO_SPACES_BUCKET=example-space
SUB2GEN_DO_SPACES_PREFIX=sub2gen/cache
SUB2GEN_DO_SPACES_CDN_BASE_URL=https://example-space.nyc3.cdn.digitaloceanspaces.com
SUB2GEN_DO_API_TOKEN=...
SUB2GEN_DO_CDN_ENDPOINT_ID=...
```

The prefix is a virtual folder represented by the beginning of each object key. It does not need to be created manually.

## Activation

Open **Manage → Cache**, select **DigitalOcean Spaces**, choose a delivery mode, and use **Test connection** before saving.

- **Private proxy** stores private objects and preserves sub2gen API-key/project authorization through `/api/cache/blob/...`.
- **Public Spaces CDN** stores `public-read` objects and returns direct CDN URLs. Anyone with such a URL can download the object.

The current cache must be empty before changing provider or delivery mode. Clear operations delete origin objects and, in CDN mode, purge the configured CDN prefix. Spaces failures never fall back to local cache.
