FROM eceasy/cli-proxy-api:v7.2.120

COPY entrypoint.sh /usr/local/bin/sub2gen-cliproxy-entrypoint
RUN chmod 0755 /usr/local/bin/sub2gen-cliproxy-entrypoint

EXPOSE 8317

ENTRYPOINT ["/usr/local/bin/sub2gen-cliproxy-entrypoint"]

