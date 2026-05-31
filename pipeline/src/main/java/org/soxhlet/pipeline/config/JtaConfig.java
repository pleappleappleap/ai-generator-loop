package org.soxhlet.pipeline.config;

import com.arjuna.ats.arjuna.common.ObjectStoreEnvironmentBean;
import com.arjuna.common.internal.util.propertyservice.BeanPopulator;
import com.zaxxer.hikari.HikariDataSource;
import jakarta.jms.XAConnectionFactory;
import org.apache.activemq.artemis.jms.client.ActiveMQXAConnectionFactory;
import org.postgresql.xa.PGXADataSource;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import javax.sql.DataSource;
import javax.sql.XADataSource;

@Configuration
public class JtaConfig {

    @Bean
    public XADataSource xaDataSource(
            @Value("${spring.datasource.url}") String url,
            @Value("${spring.datasource.username}") String username,
            @Value("${spring.datasource.password}") String password) {
        PGXADataSource ds = new PGXADataSource();
        ds.setUrl(url);
        ds.setUser(username);
        ds.setPassword(password);
        return ds;
    }

    @Bean
    public XAConnectionFactory xaConnectionFactory(
            @Value("${spring.artemis.broker-url}") String brokerUrl) {
        return new ActiveMQXAConnectionFactory(brokerUrl);
    }

    /**
     * Plain (non-XA) DataSource for Narayana's JDBC object store.
     * A separate non-XA connection is required — using an XA connection for the
     * transaction log itself would be circular. A pool of 2 is ample for the
     * prepare/commit write rate.
     *
     * Configures all three Narayana object store beans (default, communicationStore,
     * stateStore) to use this DataSource. Must run before Narayana's
     * auto-configuration initialises the transaction manager, which is guaranteed
     * because @Configuration beans are processed before @AutoConfiguration.
     */
    @Bean
    public DataSource narayanaObjectStoreDataSource(
            @Value("${spring.datasource.url}") String url,
            @Value("${spring.datasource.username}") String username,
            @Value("${spring.datasource.password}") String password) {
        HikariDataSource ds = new HikariDataSource();
        ds.setJdbcUrl(url);
        ds.setUsername(username);
        ds.setPassword(password);
        ds.setMaximumPoolSize(2);
        ds.setMinimumIdle(1);
        ds.setPoolName("narayana-os");

        for (String storeName : new String[]{null, "communicationStore", "stateStore"}) {
            ObjectStoreEnvironmentBean osBean = BeanPopulator.getNamedInstance(
                    ObjectStoreEnvironmentBean.class, storeName);
            osBean.setObjectStoreType(
                    "com.arjuna.ats.internal.arjuna.objectstore.jdbc.JDBCStore");
            osBean.setJdbcDataSource(ds);
            osBean.setTablePrefix("narayana");
            osBean.setCreateTable(true);
            osBean.setDropTable(false);
        }
        return ds;
    }
}
